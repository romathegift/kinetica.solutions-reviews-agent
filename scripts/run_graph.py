"""
Сквозной прогон агента: один отзыв через ВЕСЬ граф LangGraph,
с чекпоинтами в Postgres, остановкой на HITL-гейте и отправкой карточки
оператору в Telegram.

Запуск из корня проекта при поднятом SSH-туннеле:
    python -m scripts.run_graph rev_003                  # начать нить / пауза / карточка
    python -m scripts.run_graph rev_003 --no-telegram    # то же, но карточку не слать
    python -m scripts.run_graph rev_003 --card           # переотправить карточку существующей паузы
    python -m scripts.run_graph rev_003 approve          # ВОЗОБНОВИТЬ существующую паузу
    python -m scripts.run_graph rev_003 reject           # возобновить с отказом
    python -m scripts.run_graph rev_003 --reset          # стереть нить и прогнать заново

ЧТО ИЗМЕНИЛОСЬ НА ШАГЕ R1
-------------------------
1. Оркестрация (открыть чекпоинтер, проверить состояние нити, зайти
   в паузу) уехала в reviews_agent/hitl.py. Здесь остался CLI поверх неё.
   Ровно те же функции зовёт reviews_agent/tg/handlers.py — одна проверка
   состояния нити на оба фронтенда, а не две расходящиеся копии.

2. START теперь отправляет НАСТОЯЩУЮ карточку в Telegram. Отправка стоит
   ЗДЕСЬ, а не в узле human_gate, и это принципиально: при
   Command(resume=...) LangGraph переисполняет узел с начала, и
   send_message внутри узла продублировал бы карточку на каждое
   возобновление. Подробности — в docstring reviews_agent/tg/sender.py.

3. Режимы approve|reject сохранены как аварийный путь: они позволяют
   довести отзыв до конца, когда бот недоступен. Штатный путь решения —
   кнопки в Telegram.

4. Появился --card: переотправить карточку по существующей паузе, не
   платя за повторную генерацию. Черновик уже лежит в чекпоинте.

ТРИ РЕЖИМА РАБОТЫ С НИТЬЮ — И ЭТО ПРИНЦИПИАЛЬНО
-----------------------------------------------
  START (без второго аргумента)
      invoke(initial_state) -> граф идёт до human_gate -> interrupt()
      замораживает состояние в чекпоинт -> карточка уходит оператору ->
      процесс ВЫХОДИТ, оставив паузу в базе. Ничего не возобновляет.

  RESUME (approve|reject)
      НЕ вызывает invoke(initial_state). Читает состояние нити из базы,
      убеждается, что она стоит на human_gate, и заходит в паузу через
      Command(resume=decision). Если нить не на паузе, режим честно
      об этом говорит и не делает вид, что возобновил.

  --reset
      delete_thread(thread_id) стирает нить целиком. Нужен потому, что
      thread_id=review_id: без сброса повторные прогоны одной фикстуры
      накапливаются в одной нити и путают картину.

КРОСС-ПРОЦЕССНАЯ ПРОВЕРКА ДЕЛАЕТСЯ ТАК:
    python -m scripts.run_graph rev_003 --reset     # чистая нить
    python -m scripts.run_graph rev_003             # процесс 1: пауза + карточка
    <нажать кнопку в Telegram>                      # процесс 2: вебхук-сервис
Во втором прогоне НЕ должно быть ни warning'а про эмбеддинги, ни вызова
Sonnet: если их нет, а final_reply совпал с черновиком из процесса 1 —
пауза пережила смерть процесса, состояние поднялось из Postgres.

thread_id = review_id: одна нить чекпоинтов на отзыв.

СТОИТ ДЕНЕГ: START-прогон complaint/question — до 1 вызова Sonnet и 1 Haiku.
RESUME и --card денег не стоят: модель не вызывается, состояние берётся из базы.

НЕДЕТЕРМИНИРОВАН ПО ЧЕРНОВИКУ: тот же отзыв в новом START даст другой
текст. Это требование к генерации, не баг. RESUME же детерминирован:
он поднимает УЖЕ написанный черновик из чекпоинта.
"""

import json
import sys

from reviews_agent.config import PROJECT_ROOT
from reviews_agent.db import close_pool
from reviews_agent.hitl import (
    HitlError,
    ThreadNotFound,
    ThreadNotPaused,
    decision_approve,
    decision_reject,
    get_pause,
    reset_thread,
    resume_thread,
    start_review,
)
from reviews_agent.tg.sender import send_operator_card

REVIEWS_FILE = PROJECT_ROOT / "data" / "reviews.json"

# Решение оператора аргументом -> поля, которыми возобновляется гейт.
# approve: публикуем черновик как есть. reject: не публикуем.
# edit здесь намеренно НЕ эмулируется: правленый текст приходит из Telegram
# (handlers.on_edit_reply), и подделывать его аргументом значило бы
# проверять не тот путь, который работает в проде.
OPERATOR_DECISIONS = {
    "approve": decision_approve,
    "reject": decision_reject,
}

FLAGS = ("--reset", "--card", "--no-telegram")


def load_fixture(review_id: str) -> dict:
    """Достаёт одну фикстуру по review_id. Падает внятно, если её нет."""
    with REVIEWS_FILE.open(encoding="utf-8") as f:
        reviews = json.load(f)

    for review in reviews:
        if review["review_id"] == review_id:
            return review

    available = ", ".join(r["review_id"] for r in reviews)
    raise SystemExit(f"Фикстура {review_id!r} не найдена. Доступны: {available}")


def print_state(tag: str, state: dict) -> None:
    """Печатает срез состояния, важный для проверки маршрута."""
    print(f"\n--- {tag} ---")
    print(f"  category:      {state.get('category')}")
    print(f"  sentiment:     {state.get('sentiment')}")
    print(f"  escalate:      {state.get('escalate')}")
    if state.get("escalate"):
        print(f"  причина:       {state.get('escalation_reason')}")
    if state.get("draft"):
        print(f"  draft:         {state['draft'][:80]}...")
    flags = state.get("guardrail_flags")
    if flags:
        print(f"  guardrails:    {', '.join(flags)}")
    if state.get("review_status"):
        print(f"  review_status: {state['review_status']}")
    if state.get("final_reply"):
        print(f"  final_reply:   {state['final_reply'][:80]}...")


def print_payload(payload: dict) -> None:
    """Печатает нагрузку interrupt() — из чего собралась карточка."""
    print("    карточка оператору:")
    print(f"      draft:      {payload.get('draft', '')[:80]}...")
    print(f"      flags:      {payload.get('guardrail_flags')}")
    print(f"      escalate:   {payload.get('escalate')}")


def deliver_card(payload: dict) -> None:
    """
    Отправляет карточку и печатает результат.

    Сбой доставки НЕ роняет прогон: пауза уже сохранена в базу, отзыв не
    потерян, карточку можно переотправить режимом --card. Уронить процесс
    здесь значило бы наказать за проблему сети тем, что оператор потом
    не найдёт, чем эта нить закончилась.
    """
    try:
        message_id = send_operator_card(payload)
    except Exception as exc:  # noqa: BLE001 — сеть/Telegram/конфиг, всё одинаково нефатально
        print(f"\n[!] Карточку отправить не удалось: {exc}")
        print(
            f"    Пауза в базе сохранена. Переотправить: "
            f"python -m scripts.run_graph {payload.get('review_id')} --card"
        )
        return

    print(f"\n[📨] Карточка отправлена оператору (message_id={message_id}).")
    print("     Решение принимается кнопками в Telegram.")


def do_reset(review_id: str) -> None:
    """Стирает нить чекпоинтов этого отзыва целиком."""
    reset_thread(review_id)
    print(f"[✓] Нить {review_id!r} стёрта. Следующий прогон начнётся с чистого листа.")


def do_card(review_id: str) -> None:
    """Переотправляет карточку по существующей паузе. Модель не вызывается."""
    try:
        payload = get_pause(review_id)
    except ThreadNotFound:
        raise SystemExit(
            f"Нить {review_id!r} пуста. Сначала: python -m scripts.run_graph {review_id}"
        )
    except ThreadNotPaused as exc:
        raise SystemExit(
            f"Нить {review_id!r} не на паузе (review_status={exc.review_status!r}) — "
            f"карточку слать незачем."
        )

    print(f"[⏸] Пауза нити {review_id!r} найдена. Генерация НЕ повторяется.")
    print_payload(payload)
    deliver_card(payload)


def do_start(fixture: dict, send_card: bool) -> None:
    """
    START: прогон с нуля до паузы (или до конца на спаме) + карточка оператору.

    Возобновление здесь НЕ делается намеренно — процесс должен выйти,
    оставив паузу в базе, чтобы решение оператора пришло отдельным
    процессом (вебхуком) и доказало переживание паузы.
    """
    result = start_review(fixture)
    print_state("ФАЗА 1: после invoke", result["state"])

    if not result["next"]:
        print("\n[i] Граф завершился без паузы (спам-ветка: spam_filter -> END).")
        print(f"    Финальный review_status: {result['state'].get('review_status')!r}")
        return

    if not result["paused"]:
        print(f"\n[!] Граф встал на {result['next']}, а не на 'human_gate'.")
        print("    Это неожиданно — проверь топологию.")
        return

    print("\n[⏸] Граф на HITL-паузе перед 'human_gate'.")
    print_payload(result["payload"])

    if send_card:
        deliver_card(result["payload"])
    else:
        print("\n[i] Карточка не отправлена (--no-telegram).")

    print(
        "\n[i] Пауза сохранена в базу. Процесс выходит.\n"
        f"    Аварийное возобновление без бота: "
        f"python -m scripts.run_graph {result['review_id']} approve"
    )


def do_resume(review_id: str, decision_key: str) -> None:
    """
    RESUME: зайти в СУЩЕСТВУЮЩУЮ паузу и довести граф до конца.

    Аварийный путь на случай недоступного бота. Штатно решение приходит
    кнопкой — тот же resume_thread, только вызванный из вебхука.
    """
    try:
        payload = get_pause(review_id)
        print(f"[⏸] Нашли паузу нити {review_id!r} перед 'human_gate'.")
        print("    (стартовый прогон НЕ повторяется — состояние взято из базы)")
        print_payload(payload)

        decision = OPERATOR_DECISIONS[decision_key]()
        print(f"\n[▶] Возобновляем решением: {decision_key} -> {decision}")

        final = resume_thread(review_id, decision)

    except ThreadNotFound:
        raise SystemExit(
            f"Нить {review_id!r} пуста — возобновлять нечего.\n"
            f"Сначала запусти: python -m scripts.run_graph {review_id}"
        )
    except ThreadNotPaused as exc:
        raise SystemExit(
            f"Нить {review_id!r} не на паузе human_gate "
            f"(next={exc.next_nodes}, review_status={exc.review_status!r}).\n"
            f"Похоже, она уже завершена. Для чистого прогона: "
            f"python -m scripts.run_graph {review_id} --reset"
        )
    except HitlError as exc:
        raise SystemExit(str(exc))

    print_state("ФАЗА 2: после resume", final)

    print("\n" + "=" * 78)
    print(f"ГОТОВО. Финальный review_status: {final.get('review_status')!r}")
    if final.get("final_reply"):
        print(f"К публикации:\n  {final['final_reply']}")
    else:
        print("Публиковать нечего (reject/skip).")
    print("=" * 78)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "Укажи фикстуру:\n"
            "  python -m scripts.run_graph <review_id>                # начать / пауза / карточка\n"
            "  python -m scripts.run_graph <review_id> --no-telegram  # то же, без карточки\n"
            "  python -m scripts.run_graph <review_id> --card         # переотправить карточку\n"
            "  python -m scripts.run_graph <review_id> approve        # возобновить\n"
            "  python -m scripts.run_graph <review_id> reject         # возобновить с отказом\n"
            "  python -m scripts.run_graph <review_id> --reset        # стереть нить"
        )

    review_id = sys.argv[1]
    second = sys.argv[2] if len(sys.argv) > 2 else None

    if second is not None and second not in (*OPERATOR_DECISIONS, *FLAGS):
        raise SystemExit(
            f"Неизвестный аргумент {second!r}. Допустимо: "
            f"{', '.join(OPERATOR_DECISIONS)}, {', '.join(FLAGS)}."
        )

    print("=" * 78)
    print(f"ПРОГОН ГРАФА: {review_id}  (режим: {second or 'start'})")
    print("=" * 78)

    if second == "--reset":
        do_reset(review_id)
        return

    if second == "--card":
        do_card(review_id)
        return

    if second in OPERATOR_DECISIONS:
        do_resume(review_id, second)
        return

    fixture = load_fixture(review_id)
    print(f"  отзыв: {fixture['text'][:70]}...")
    do_start(fixture, send_card=(second != "--no-telegram"))


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()