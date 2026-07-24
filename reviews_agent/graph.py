"""
Сборка графа LangGraph: узлы, маршрутизация, HITL-гейт, чекпоинты, отказы.

Топология
---------
    ingest -> classify -> route_by_category ┬─ spam_filter ─────────────┐
                                            ├─ generate ────────────────┤ (praise)
                                            └─ retrieve -> generate ─────┤ (complaint/question)
                                                                         │
    generate -> [interrupt: HITL-гейт] -> finalize -> END               │
    spam_filter ─────────────────────────────────────────────────────> END

    classify | retrieve | generate --(ретраи исчерпаны)--> failed -> END

Ключевые решения этого модуля
-----------------------------
1. Роутер — УСЛОВНОЕ РЕБРО, а не узел. route_by_category возвращает готовое
   имя узла; add_conditional_edges маппит его один-в-один. Держать имена
   в одном месте (router.NODE_*) нельзя частично — граф падает на компиляции
   при первом же расхождении, и это правильно: опечатка ловится на сборке.

2. Checkpointer держит СВОЁ соединение, отдельно от пула проекта.
   Пул (db.get_pool) настроен callback'ом register_vector и работает
   в дефолтном режиме — tuple row_factory, транзакционный. PostgresSaver 3.1.0
   требует dict_row и autocommit для setup(). Посадить его на общий пул
   значит уронить чекпоинт. from_conn_string открывает коннект с нужным
   режимом сам. Строка подключения — та же settings.postgres_dsn, тот же
   Postgres через тот же SSH-туннель: два коннекта, разные задачи.

3. interrupt() стоит ПОСЛЕ generate, внутри отдельного узла human_gate.
   Граф доходит до него, замораживает состояние в чекпоинт и ОТДАЁТ
   управление — процесс может умереть, пауза на ревью оператора длится
   часами. Возобновление — Command(resume=decision) по тому же thread_id.
   Сам Telegram здесь НЕ вызывается: карточку шлёт вызывающий код (шаг
   с интеграцией), гейт лишь размечает точку паузы и превращает решение
   оператора в поля состояния.

4. errors инициализируется пустым списком на входе (make_initial_state).
   Поле снабжено reducer'ом operator.add — он применяется к СУЩЕСТВУЮЩЕМУ
   значению. Без инициализации первая же ошибка узла применила бы add
   к отсутствующему ключу.

5. РЕТРАИ РАЗДЕЛЕНЫ ПО СЛОЯМ, а не сложены друг на друга.
   Под каждым внешним вызовом УЖЕ есть свой механизм повторов, и это
   выяснилось замерами, а не из документации:

   - SDK anthropic ретраит сам, max_retries по умолчанию 2 — до 3 обращений
     к API на один вызов узла. RetryPolicy(max_attempts=3) поверх дала бы
     до 9 запросов на отзыв и наложение двух бэкоффов: при 429 мы били бы
     в лимит втрое чаще, чем думаем.
   - psycopg_pool переподключается сам внутри своего timeout (db.POOL_TIMEOUT)
     и только исчерпав его бросает PoolTimeout — который, к слову, является
     подклассом psycopg.OperationalError, поэтому retry_on ниже его ловит.

   ЗАМЕР 2026-07-24: при недоступном Postgres и трёх попытках графа поверх
   дефолтных 30 секунд пула отказ доходил до оператора за 97 секунд. После
   правки (пул 10 секунд, две попытки здесь) — порядка 20.

   Правило, выведенное из обоих случаев: повторы принадлежат тому слою,
   который видит больше всего информации об отказе. SDK читает заголовки
   ответа и ждёт столько, сколько просит сервер; пул знает состояние
   соединений. Слой графа сверху — только страховка от длинной просадки,
   и попыток у него минимум.

6. ValueError НЕ РЕТРАИТСЯ НИГДЕ. В classify он означает «модель вернула
   мусор», в retrieve — «баг маршрутизации». При temperature=0 повтор
   вернёт ровно тот же мусор, а баг маршрутизации повтором не лечится.
   Дефолтный retry_on LangGraph исключает ValueError сам; мы это не
   переопределяем и полагаемся на явные списки исключений.

7. timeout= на add_node НЕ ИСПОЛЬЗУЕТСЯ. Он поддерживается только для
   async-узлов; на синхронном узле граф падает при компиляции. Все узлы
   здесь синхронные. Появится async — можно вернуться к этому.

8. error_handler ведёт в отдельный терминальный узел failed, а не в END
   напрямую и уж точно не дальше по маршруту. Упавший classify оставляет
   состояние без category, упавший generate — без draft; human_gate на
   таком состоянии упал бы на state["draft"], подменив исходную ошибку
   вторичной. failed существует ещё и для трассировки: по чекпоинту
   должно быть видно, что нить закончилась отказом, а не решением.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import anthropic
import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from reviews_agent.nodes.classify import classify
from reviews_agent.nodes.generate import generate
from reviews_agent.nodes.ingest import ingest
from reviews_agent.nodes.retrieve import retrieve
from reviews_agent.nodes.router import (
    NODE_GENERATE,
    NODE_RETRIEVE,
    NODE_SPAM_FILTER,
    route_by_category,
)
from reviews_agent.nodes.spam_filter import spam_filter
from reviews_agent.state import ProcessingError, ReviewState
from reviews_agent.tg.sender import send_failure_notice

logger = logging.getLogger(__name__)

# --- Имена узлов, которых нет в router.py (роутер знает только свои цели) ---
NODE_INGEST = "ingest"
NODE_CLASSIFY = "classify"
NODE_HUMAN_GATE = "human_gate"
NODE_FINALIZE = "finalize"
NODE_FAILED = "failed"


# ======================================================================
# Политики ретраев
# ======================================================================
# Транзиентные отказы Anthropic API. Перечисляем явно, а не полагаемся
# на дефолт: дефолтный retry_on для http-библиотек ретраит только 5xx,
# а 429 (рейт-лимит) — не 5xx, и именно он у нас самый частый.
ANTHROPIC_TRANSIENT = (
    anthropic.RateLimitError,       # 429
    anthropic.APIConnectionError,   # сеть; APITimeoutError — его подкласс
    anthropic.APITimeoutError,
    anthropic.InternalServerError,  # 5xx
)

# Тонкая надстройка над собственными ретраями SDK (см. решение 5).
# max_attempts=2 — это ВТОРОЙ заход после того, как SDK исчерпал свои три.
RETRY_LLM = RetryPolicy(
    max_attempts=2,
    initial_interval=2.0,
    backoff_factor=2.0,
    max_interval=30.0,
    jitter=True,
    retry_on=ANTHROPIC_TRANSIENT,
)

# Надстройка над переподключениями пула (db.POOL_TIMEOUT).
#
# ДВЕ попытки, а не три: пул уже потратил на переподключение свой таймаут,
# и каждая лишняя попытка здесь стоит его целиком. Замер до правки —
# 97 секунд до уведомления оператора; это и был случай «три попытки
# поверх тридцати секунд».
#
# Смысл этого слоя остаётся ровно один: обрыв коннекта ПОСРЕДИ запроса
# пул не покрывает — он выдал живое соединение, а упало оно уже в работе.
# Повтор здесь получит из пула другое и пройдёт быстро.
#
# OperationalError покрывает и обрыв, и PoolTimeout (он его подкласс).
# ProgrammingError (кривой SQL) сюда намеренно не входит.
RETRY_DB = RetryPolicy(
    max_attempts=2,
    initial_interval=1.0,
    backoff_factor=2.0,
    max_interval=10.0,
    jitter=True,
    retry_on=psycopg.OperationalError,
)


# ======================================================================
# Обработка окончательного отказа узла
# ======================================================================
def make_error_handler(node_name: str):
    """
    Строит error_handler для конкретного узла.

    Фабрика, а не одна общая функция с чтением имени узла из NodeError:
    имя замыкается на этапе сборки графа и заведомо верно. Полагаться на
    внутреннее устройство объекта ошибки ради того, что мы и так знаем
    в точке регистрации, — лишняя связанность.

    Возвращаемый обработчик срабатывает ПОСЛЕ того, как ретраи исчерпаны.
    Он делает три вещи: пишет запись в errors, уведомляет оператора
    и уводит граф в терминальный узел failed.
    """

    def handle_failure(state: ReviewState, error: NodeError) -> Command:
        message = str(error)
        review_id = state.get("review_id", "?")

        entry = ProcessingError(
            node=node_name,
            message=message,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        logger.error(
            "Узел %s окончательно упал на отзыве %s: %s", node_name, review_id, message
        )

        # Сбой доставки уведомления НЕ должен подменить исходную ошибку:
        # если Telegram недоступен, в состоянии всё равно обязана остаться
        # запись о том, из-за чего упал узел. Поэтому ловим широко и пишем
        # обе ошибки, а не даём второй затереть первую.
        try:
            send_failure_notice(
                review_id=review_id,
                node=node_name,
                message=message,
                review_text=state.get("text", ""),
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить оператора об отказе узла %s (отзыв %s)",
                node_name,
                review_id,
            )

        return Command(
            update={
                "errors": [entry],
                # skipped — единственный статус из ReviewStatus, означающий
                # «ответа не будет». Оставить поле пустым нельзя: снаружи
                # (hitl.resume_thread) отсутствие статуса читается как
                # «нить ещё в работе».
                "review_status": "skipped",
            },
            goto=NODE_FAILED,
        )

    return handle_failure


# ======================================================================
# HITL-гейт
# ======================================================================
def human_gate(state: ReviewState) -> ReviewState:
    """
    Точка паузы на решение оператора.

    interrupt() выбрасывает управление наружу и замораживает состояние
    в чекпоинт. Значение, переданное в interrupt(), — полезная нагрузка
    для вызывающего кода: из чего собрать карточку оператору (черновик,
    флаги гардрейлов, эскалация). Отдаём ровно то, что нужно для решения,
    и ничего лишнего: msgpack-строгий режим не пропустит сложные типы.

    Когда вызывающий код возобновляет граф через Command(resume=decision),
    именно этот decision становится возвращаемым значением interrupt().
    Ожидаемая форма decision — dict:
        {"review_status": "approved"|"rejected"|"edited"|"skipped",
         "final_reply": "<текст, если оператор правил; иначе черновик>"}

    Валидацию решения намеренно держим здесь, а не в finalize: некорректный
    resume должен упасть в точке возобновления, а не протечь дальше по графу.
    """
    decision: dict[str, Any] = interrupt(
        {
            "review_id": state["review_id"],
            "text": state["text"],
            "category": state["category"],
            "sentiment": state["sentiment"],
            "urgency": state["urgency"],
            "escalate": state.get("escalate", False),
            "escalation_reason": state.get("escalation_reason", ""),
            "draft": state["draft"],
            "guardrail_flags": state.get("guardrail_flags", []),
        }
    )

    status = decision.get("review_status")
    valid_statuses = ("approved", "rejected", "edited", "skipped")
    if status not in valid_statuses:
        raise ValueError(
            f"human_gate получил недопустимый review_status: {status!r}. "
            f"Ожидалось одно из {valid_statuses}."
        )

    # Оператор мог отредактировать черновик — тогда в final_reply лежит
    # правленый текст. Если не правил (approved как есть), берём черновик.
    # rejected/skipped: ответа не будет, final_reply пустой — это норма,
    # публиковать нечего.
    if status in ("approved", "edited"):
        final_reply = decision.get("final_reply") or state["draft"]
    else:
        final_reply = ""

    return {"review_status": status, "final_reply": final_reply}


# ======================================================================
# Финализация
# ======================================================================
def finalize(state: ReviewState) -> ReviewState:
    """
    Терминальный узел одобренных/правленых веток.

    Сейчас — чистая точка схождения после гейта: состояние уже полное
    (review_status + final_reply проставлены в human_gate). Публикация
    ответа наружу (постинг на площадку-источник) придёт с интеграцией;
    здесь для неё зарезервировано место, чтобы топология не менялась,
    когда постинг появится.

    Узел существует отдельно от human_gate, чтобы точка "решение принято"
    и точка "ответ обработан на выходе" не сливались в одну — по чекпоинту
    их нужно различать.
    """
    return {}


# ======================================================================
# Терминатор отказа
# ======================================================================
def failed(state: ReviewState) -> ReviewState:
    """
    Терминальный узел ветки отказа.

    Пустой намеренно: всю работу (запись в errors, уведомление оператора,
    выставление статуса) уже сделал error_handler. Узел нужен как ЯВНАЯ
    точка на графе — по чекпоинту должно быть видно, что нить закончилась
    отказом, а не решением оператора, и на нём же будет вешаться алерт
    админу, когда на R3 появятся логи контейнера.
    """
    return {}


# ======================================================================
# Инициализация состояния
# ======================================================================
def make_initial_state(review: dict[str, Any]) -> ReviewState:
    """
    Собирает стартовое состояние из входного отзыва.

    Единственная точка, где errors инициализируется пустым списком:
    reducer operator.add применяется к существующему значению, поэтому
    ключ обязан существовать ДО первой возможной ошибки. Прятать это
    в узлах нельзя — узел с ошибкой может отработать раньше любого,
    кто "по пути" завёл бы список.

    client_id СЮДА НЕ КЛАДЁТСЯ намеренно. Его проставляет classify из
    настроек (state.get("client_id") or settings.client_id), а Retrieve
    и Generate читают из состояния. Подсунуть client_id здесь значит
    скрыть поломку этого пути: если classify однажды перестанет его
    ставить, Retrieve упадёт на state["client_id"] — и это правильный,
    громкий отказ, а не тихая работа на дефолте. Тот же инвариант, что
    в scripts/preview_generate.py.

    _expected из фикстуры не попадает в состояние: это ожидания теста,
    а не данные отзыва. Фильтруется на входе.

    Ожидаемые ключи review — поля входа из ReviewState:
        review_id, source, author, rating, created_at, text.
    """
    return {
        "review_id": review["review_id"],
        "source": review["source"],
        "author": review["author"],
        "rating": review["rating"],
        "created_at": review["created_at"],
        "text": review["text"],
        "errors": [],
    }


# ======================================================================
# Сборка графа
# ======================================================================
def build_graph() -> StateGraph:
    """
    Определяет узлы и рёбра. Возвращает НЕскомпилированный граф —
    компиляция с конкретным checkpointer отдельным шагом (compile_graph),
    чтобы граф можно было собрать в тестах без базы.

    Политики ретраев и обработчики отказа вешаются ЗДЕСЬ, а не внутри
    узлов: узлы обязаны падать честно, чтобы механизм отработал. Так это
    и было заложено в докстринге classify.

    ingest и spam_filter обвязки не получают: у первого нет внешних
    вызовов вообще, у второго — тоже (он только размечает спам).
    """
    graph = StateGraph(ReviewState)

    # --- Узлы без внешних вызовов: ретраить нечего ---
    graph.add_node(NODE_INGEST, ingest)
    graph.add_node(NODE_SPAM_FILTER, spam_filter)

    # --- Узлы с внешними вызовами ---
    graph.add_node(
        NODE_CLASSIFY,
        classify,
        retry_policy=RETRY_LLM,
        error_handler=make_error_handler(NODE_CLASSIFY),
    )
    graph.add_node(
        NODE_RETRIEVE,
        retrieve,
        retry_policy=RETRY_DB,
        error_handler=make_error_handler(NODE_RETRIEVE),
    )
    graph.add_node(
        NODE_GENERATE,
        generate,
        retry_policy=RETRY_LLM,
        error_handler=make_error_handler(NODE_GENERATE),
    )

    # --- Гейт, финализация, терминатор отказа ---
    graph.add_node(NODE_HUMAN_GATE, human_gate)
    graph.add_node(NODE_FINALIZE, finalize)
    graph.add_node(NODE_FAILED, failed)

    # --- Линейный вход ---
    graph.add_edge(START, NODE_INGEST)
    graph.add_edge(NODE_INGEST, NODE_CLASSIFY)

    # --- Развилка по категории. Роутер вернёт одно из трёх имён узлов;
    #     маппинг тождественный (метка == узел), но пишем его явно —
    #     так на графе видно все достижимые цели ветвления. ---
    graph.add_conditional_edges(
        NODE_CLASSIFY,
        route_by_category,
        {
            NODE_RETRIEVE: NODE_RETRIEVE,
            NODE_GENERATE: NODE_GENERATE,
            NODE_SPAM_FILTER: NODE_SPAM_FILTER,
        },
    )

    # --- complaint/question: контекст -> генерация ---
    graph.add_edge(NODE_RETRIEVE, NODE_GENERATE)

    # --- Генерация -> гейт оператора -> финализация ---
    graph.add_edge(NODE_GENERATE, NODE_HUMAN_GATE)
    graph.add_edge(NODE_HUMAN_GATE, NODE_FINALIZE)
    graph.add_edge(NODE_FINALIZE, END)

    # --- Спам обрывается на своём терминаторе ---
    graph.add_edge(NODE_SPAM_FILTER, END)

    # --- Ветка отказа. Входит через Command(goto=...) из error_handler,
    #     обычного ребра сюда нет: в failed попадают только упавшие узлы. ---
    graph.add_edge(NODE_FAILED, END)

    return graph


def compile_graph(checkpointer: PostgresSaver):
    """
    Компилирует граф с переданным checkpointer.

    Checkpointer инъектируется, а не создаётся внутри: сборка графа не
    должна открывать соединение с базой. Владение коннектом остаётся
    у вызывающего (run_graph держит его контекст-менеджером открытым
    на всё время работы).
    """
    return build_graph().compile(checkpointer=checkpointer)