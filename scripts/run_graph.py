"""
Сквозной прогон агента: один отзыв через ВЕСЬ граф LangGraph,
с чекпоинтами в Postgres и остановкой на HITL-гейте.

Запуск из корня проекта при поднятом SSH-туннеле:
    python -m scripts.run_graph rev_003            # начать нить / встать на паузу
    python -m scripts.run_graph rev_003 approve    # ВОЗОБНОВИТЬ существующую паузу
    python -m scripts.run_graph rev_003 reject     # возобновить с отказом
    python -m scripts.run_graph rev_003 --reset     # стереть нить и прогнать заново

ТРИ РЕЖИМА — И ЭТО ПРИНЦИПИАЛЬНО
-------------------------------
Раньше resume работал только внутри ОДНОГО процесса: invoke(initial) ->
пауза -> resume подряд. Такой сценарий не может доказать, что пауза
переживает смерть процесса, — оба конца в одной памяти.

Теперь режимы разведены:

  START (без второго аргумента)
      invoke(initial_state) -> граф идёт до human_gate -> interrupt()
      замораживает состояние в чекпоинт -> процесс ВЫХОДИТ, оставив паузу
      в базе. Ничего не возобновляет.

  RESUME (approve|reject)
      НЕ вызывает invoke(initial_state). Читает состояние нити из базы,
      убеждается, что она стоит на human_gate, и заходит в паузу через
      Command(resume=decision). Стартовый прогон здесь не повторяется —
      если нить не на паузе, режим честно об этом говорит и не делает
      вид, что возобновил.

  --reset
      delete_thread(thread_id) стирает нить целиком. Нужен потому, что
      thread_id=review_id: без сброса повторные прогоны одной фикстуры
      накапливаются в одной нити и путают картину (ровно это случилось
      с rev_004 при отладке).

КРОСС-ПРОЦЕССНАЯ ПРОВЕРКА ДЕЛАЕТСЯ ТАК:
    python -m scripts.run_graph rev_003 --reset     # чистая нить
    python -m scripts.run_graph rev_003             # процесс 1: пауза
    python -m scripts.run_graph rev_003 approve     # процесс 2: заходит в паузу
Во втором прогоне НЕ должно быть ни warning'а про эмбеддинги, ни вызова
Sonnet: если их нет, а final_reply совпал с черновиком из процесса 1 —
пауза пережила смерть процесса, состояние поднялось из Postgres.

thread_id = review_id: одна нить чекпоинтов на отзыв.

СТОИТ ДЕНЕГ: START-прогон complaint/question — до 1 вызова Sonnet и 1 Haiku.
RESUME денег не стоит: модель не вызывается, состояние берётся из базы.

НЕДЕТЕРМИНИРОВАН ПО ЧЕРНОВИКУ: тот же отзыв в новом START даст другой
текст. Это требование к генерации, не баг. RESUME же детерминирован:
он поднимает УЖЕ написанный черновик из чекпоинта.
"""

import json
import sys

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from reviews_agent.config import PROJECT_ROOT, get_settings
from reviews_agent.db import close_pool
from reviews_agent.graph import NODE_HUMAN_GATE, compile_graph, make_initial_state

REVIEWS_FILE = PROJECT_ROOT / "data" / "reviews.json"

# Решение оператора аргументом -> поля, которыми возобновляется гейт.
# approve: публикуем черновик как есть. reject: не публикуем.
# edit здесь не эмулируем — правленый текст пришёл бы из Telegram,
# а не из аргумента; для сквозной проверки топологии хватает двух исходов.
OPERATOR_DECISIONS = {
    "approve": {"review_status": "approved"},
    "reject": {"review_status": "rejected"},
}


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


def print_operator_card(snapshot) -> None:
    """Печатает полезную нагрузку interrupt() — из чего собралась бы карточка."""
    for task in snapshot.tasks:
        for intr in task.interrupts:
            payload = intr.value
            print("    карточка оператору:")
            print(f"      draft:      {payload.get('draft', '')[:80]}...")
            print(f"      flags:      {payload.get('guardrail_flags')}")
            print(f"      escalate:   {payload.get('escalate')}")


def do_reset(checkpointer: PostgresSaver, review_id: str) -> None:
    """Стирает нить чекпоинтов этого отзыва целиком."""
    checkpointer.delete_thread(review_id)
    print(f"[✓] Нить {review_id!r} стёрта. Следующий прогон начнётся с чистого листа.")


def do_start(graph, config: dict, fixture: dict) -> None:
    """
    START: прогон с нуля до паузы (или до конца на спаме).

    Возобновление здесь НЕ делается намеренно — процесс должен выйти,
    оставив паузу в базе, чтобы отдельный процесс мог доказать её
    переживание. Возобновляют режимом approve|reject.
    """
    result = graph.invoke(make_initial_state(fixture), config)
    print_state("ФАЗА 1: после invoke", result)

    snapshot = graph.get_state(config)
    next_nodes = snapshot.next

    if not next_nodes:
        print("\n[i] Граф завершился без паузы (спам-ветка: spam_filter -> END).")
        print(f"    Финальный review_status: {result.get('review_status')!r}")
        return

    if NODE_HUMAN_GATE not in next_nodes:
        print(f"\n[!] Граф встал на {next_nodes}, а не на {NODE_HUMAN_GATE!r}.")
        print("    Это неожиданно — проверь топологию.")
        return

    print(f"\n[⏸] Граф на HITL-паузе перед {NODE_HUMAN_GATE!r}.")
    print_operator_card(snapshot)
    print(
        "\n[i] Пауза сохранена в базу. Процесс выходит.\n"
        f"    Возобновить (можно из другого процесса): "
        f"python -m scripts.run_graph {config['configurable']['thread_id']} approve"
    )


def do_resume(graph, config: dict, decision_key: str) -> None:
    """
    RESUME: зайти в СУЩЕСТВУЮЩУЮ паузу и довести граф до конца.

    invoke(initial_state) здесь не вызывается — стартовый прогон не
    повторяется. Если нить не на паузе (не начата, или уже дошла до END),
    режим честно об этом сообщает: делать вид, что возобновил то, чего
    нет, нельзя.
    """
    review_id = config["configurable"]["thread_id"]
    snapshot = graph.get_state(config)

    # Нить пуста -> START ещё не запускался.
    if not snapshot.created_at:
        raise SystemExit(
            f"Нить {review_id!r} пуста — возобновлять нечего.\n"
            f"Сначала запусти: python -m scripts.run_graph {review_id}"
        )

    # Нить есть, но не на паузе -> уже завершена (или встала не там).
    if NODE_HUMAN_GATE not in snapshot.next:
        done = snapshot.values.get("review_status")
        raise SystemExit(
            f"Нить {review_id!r} не на паузе human_gate (next={snapshot.next}, "
            f"review_status={done!r}).\n"
            f"Похоже, она уже завершена. Для чистого прогона: "
            f"python -m scripts.run_graph {review_id} --reset"
        )

    print(f"[⏸] Нашли паузу нити {review_id!r} перед {NODE_HUMAN_GATE!r}.")
    print("    (стартовый прогон НЕ повторяется — состояние взято из базы)")
    print_operator_card(snapshot)

    decision = OPERATOR_DECISIONS[decision_key]
    print(f"\n[▶] Возобновляем решением: {decision_key} -> {decision}")

    final = graph.invoke(Command(resume=decision), config)
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
            "  python -m scripts.run_graph <review_id>            # начать / пауза\n"
            "  python -m scripts.run_graph <review_id> approve    # возобновить\n"
            "  python -m scripts.run_graph <review_id> reject     # возобновить с отказом\n"
            "  python -m scripts.run_graph <review_id> --reset     # стереть нить"
        )

    review_id = sys.argv[1]
    second = sys.argv[2] if len(sys.argv) > 2 else None

    if second is not None and second not in (*OPERATOR_DECISIONS, "--reset"):
        raise SystemExit(
            f"Неизвестный аргумент {second!r}. Допустимо: "
            f"{', '.join(OPERATOR_DECISIONS)}, --reset."
        )

    settings = get_settings()
    config = {"configurable": {"thread_id": review_id}}

    print("=" * 78)
    print(f"ПРОГОН ГРАФА: {review_id}  (режим: {second or 'start'})")
    print("=" * 78)

    with PostgresSaver.from_conn_string(settings.postgres_dsn) as checkpointer:
        # --reset не компилирует граф и не читает фикстуру: чистит и выходит.
        if second == "--reset":
            do_reset(checkpointer, review_id)
            return

        graph = compile_graph(checkpointer)

        if second in OPERATOR_DECISIONS:
            # RESUME: фикстура не нужна — состояние берём из нити.
            do_resume(graph, config, second)
        else:
            # START: читаем фикстуру, прогоняем с нуля до паузы.
            fixture = load_fixture(review_id)
            print(f"  отзыв: {fixture['text'][:70]}...")
            do_start(graph, config, fixture)


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()