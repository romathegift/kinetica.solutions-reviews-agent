"""
Мост между графом и любым фронтендом решения оператора (CLI, Telegram, будущий API).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ
----------------------
До этого шага логика «прочитать нить, убедиться, что она на паузе, зайти
в неё через Command(resume=...)» жила внутри scripts/run_graph.py. С
появлением Telegram те же операции нужны вебхук-сервису. Продублировать
их значило бы завести два места, где может разъехаться проверка состояния
нити, — а именно эта проверка отделяет «возобновили паузу» от «молча
запустили граф заново».

Поэтому оркестрация переехала сюда, а run_graph.py стал тонким CLI поверх
этого модуля. Ровно те же функции зовёт telegram/handlers.py.

ГРАНИЦА ОТВЕТСТВЕННОСТИ
-----------------------
Здесь НЕТ ничего про Telegram. Модуль знает про граф и про решение оператора
в виде dict — и всё. Telegram импортирует hitl, hitl не импортирует Telegram.
Обратная зависимость превратила бы CLI в заложника вебхук-стека.

ПОЧЕМУ ИСКЛЮЧЕНИЯ, А НЕ SystemExit
----------------------------------
run_graph.py раньше падал через SystemExit — для CLI это уместно. Для
вебхука недопустимо: Telegram ретраит доставку апдейта, и повторный
callback обязан получить внятный ответ «уже обработано», а не уронить
обработчик. Поэтому состояния нити выражены исключениями, а решение
«печатать и выйти» или «ответить оператору» принимает вызывающий.

СОЕДИНЕНИЕ С БАЗОЙ
------------------
Каждый вызов открывает СВОЙ коннект чекпоинтера (from_conn_string) и
закрывает его на выходе. Для HITL это дёшево: решения оператора приходят
единицами в час, а не тысячами в секунду. Пул для чекпоинтера — задача
шага R3 (деплой), где будет понятна форма процесса; гадать сейчас нельзя.
"""

from contextlib import contextmanager
from typing import Any, Iterator

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from reviews_agent.config import get_settings
from reviews_agent.graph import NODE_HUMAN_GATE, compile_graph, make_initial_state

# --- Статусы решения оператора (те же, что валидирует human_gate) ---
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EDITED = "edited"


# ======================================================================
# Исключения состояния нити
# ======================================================================
class HitlError(Exception):
    """Базовое: с нитью нельзя сделать то, о чём попросили."""


class ThreadNotFound(HitlError):
    """Нить пуста — START по этому review_id ещё не запускался."""

    def __init__(self, review_id: str) -> None:
        self.review_id = review_id
        super().__init__(
            f"Нить {review_id!r} пуста — возобновлять нечего. "
            f"Сначала нужен START."
        )


class ThreadNotPaused(HitlError):
    """
    Нить существует, но не стоит на human_gate.

    Практически всегда это значит «уже обработана»: оператор нажал кнопку
    дважды, или Telegram переслал апдейт повторно. review_status несёт
    прежнее решение — его и показываем оператору вместо ошибки.
    """

    def __init__(self, review_id: str, review_status: Any, next_nodes: tuple) -> None:
        self.review_id = review_id
        self.review_status = review_status
        self.next_nodes = next_nodes
        super().__init__(
            f"Нить {review_id!r} не на паузе human_gate "
            f"(next={next_nodes}, review_status={review_status!r})."
        )


# ======================================================================
# Решения оператора
# ======================================================================
def decision_approve() -> dict[str, Any]:
    """Опубликовать черновик как есть."""
    return {"review_status": STATUS_APPROVED}


def decision_reject() -> dict[str, Any]:
    """Не публиковать ничего."""
    return {"review_status": STATUS_REJECTED}


def decision_edit(final_reply: str) -> dict[str, Any]:
    """
    Опубликовать текст, который написал оператор.

    Пустую правку не пропускаем: human_gate при пустом final_reply
    откатится на черновик, и правка молча потеряется — оператор увидит
    опубликованным ровно то, что правил. Лучше отказать явно.
    """
    text = (final_reply or "").strip()
    if not text:
        raise ValueError("Правка пустая — нечего публиковать.")
    return {"review_status": STATUS_EDITED, "final_reply": text}


# ======================================================================
# Доступ к графу
# ======================================================================
@contextmanager
def open_graph() -> Iterator[tuple[Any, PostgresSaver]]:
    """
    Открывает чекпоинтер и отдаёт скомпилированный граф.

    Владение коннектом остаётся здесь: он живёт ровно столько, сколько
    длится операция. Вызывающий не может случайно утечь соединением.
    """
    settings = get_settings()
    with PostgresSaver.from_conn_string(settings.postgres_dsn) as checkpointer:
        yield compile_graph(checkpointer), checkpointer


def thread_config(review_id: str) -> dict[str, Any]:
    """thread_id = review_id: одна нить чекпоинтов на отзыв."""
    return {"configurable": {"thread_id": review_id}}


def extract_interrupt_payload(snapshot) -> dict[str, Any] | None:
    """
    Достаёт полезную нагрузку interrupt() из снапшота состояния.

    Это ровно то, что human_gate передал в interrupt(): всё нужное для
    карточки оператору. Возвращает None, если нить не на паузе.
    """
    for task in snapshot.tasks:
        for intr in task.interrupts:
            return dict(intr.value)
    return None


# ======================================================================
# Операции над нитью
# ======================================================================
def start_review(review: dict[str, Any]) -> dict[str, Any]:
    """
    Прогон отзыва с нуля до паузы (или до конца на спам-ветке).

    Возвращает:
        {
          "review_id": str,
          "state":     dict,          # состояние после invoke
          "paused":    bool,          # встали на human_gate
          "next":      tuple[str],    # куда граф пойдёт дальше
          "payload":   dict | None,   # нагрузка interrupt() для карточки
        }

    Карточку в Telegram отсюда НЕ шлём. Отправка — дело вызывающего:
    у CLI и у вебхука разные требования к тому, что делать при сбое
    доставки, и зашивать одно из них в оркестрацию нельзя.

    СТОИТ ДЕНЕГ: complaint/question — до 1 вызова Haiku и 1 Sonnet.
    """
    review_id = review["review_id"]
    config = thread_config(review_id)

    with open_graph() as (graph, _):
        state = graph.invoke(make_initial_state(review), config)
        snapshot = graph.get_state(config)
        next_nodes = tuple(snapshot.next)
        paused = NODE_HUMAN_GATE in next_nodes

        return {
            "review_id": review_id,
            "state": state,
            "paused": paused,
            "next": next_nodes,
            "payload": extract_interrupt_payload(snapshot) if paused else None,
        }


def get_pause(review_id: str) -> dict[str, Any]:
    """
    Читает нагрузку паузы БЕЗ возобновления графа.

    Нужен, чтобы переотправить карточку оператору (потерялась, бот молчал,
    сервис лежал) не платя за повторную генерацию: черновик уже в чекпоинте.

    Модель не вызывается. Денег не стоит.
    """
    config = thread_config(review_id)

    with open_graph() as (graph, _):
        snapshot = graph.get_state(config)

        if not snapshot.created_at:
            raise ThreadNotFound(review_id)

        next_nodes = tuple(snapshot.next)
        if NODE_HUMAN_GATE not in next_nodes:
            raise ThreadNotPaused(
                review_id, snapshot.values.get("review_status"), next_nodes
            )

        payload = extract_interrupt_payload(snapshot)
        if payload is None:
            # Нить стоит перед human_gate, но interrupt-нагрузки нет.
            # Это внутреннее противоречие, а не пользовательская ситуация.
            raise HitlError(
                f"Нить {review_id!r} на паузе перед {NODE_HUMAN_GATE!r}, "
                f"но полезная нагрузка interrupt() отсутствует."
            )
        return payload


def resume_thread(review_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """
    Заходит в СУЩЕСТВУЮЩУЮ паузу и доводит граф до конца.

    invoke(initial_state) здесь не вызывается — стартовый прогон не
    повторяется. Состояние поднимается из Postgres, поэтому вызов
    детерминирован и не стоит денег: черновик уже написан.

    Бросает ThreadNotFound / ThreadNotPaused. Вызывающий решает, что это
    для него значит: для CLI — сообщение и выход, для Telegram — «уже
    обработано» оператору.
    """
    config = thread_config(review_id)

    with open_graph() as (graph, _):
        snapshot = graph.get_state(config)

        if not snapshot.created_at:
            raise ThreadNotFound(review_id)

        next_nodes = tuple(snapshot.next)
        if NODE_HUMAN_GATE not in next_nodes:
            raise ThreadNotPaused(
                review_id, snapshot.values.get("review_status"), next_nodes
            )

        return graph.invoke(Command(resume=decision), config)


def reset_thread(review_id: str) -> None:
    """
    Стирает нить чекпоинтов целиком.

    Нужен потому, что thread_id=review_id: без сброса повторные прогоны
    одной фикстуры накапливаются в одной нити. Компилировать граф для
    этого не требуется — чистит сам чекпоинтер.
    """
    settings = get_settings()
    with PostgresSaver.from_conn_string(settings.postgres_dsn) as checkpointer:
        checkpointer.delete_thread(review_id)