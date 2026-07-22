"""
Сборка графа LangGraph: узлы, маршрутизация, HITL-гейт, чекпоинты.

Топология
---------
    ingest -> classify -> route_by_category ┬─ spam_filter ─────────────┐
                                            ├─ generate ────────────────┤ (praise)
                                            └─ retrieve -> generate ─────┤ (complaint/question)
                                                                         │
    generate -> [interrupt: HITL-гейт] -> finalize -> END               │
    spam_filter ─────────────────────────────────────────────────────> END

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
"""

from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

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
from reviews_agent.state import ReviewState

# --- Имена узлов, которых нет в router.py (роутер знает только свои цели) ---
NODE_INGEST = "ingest"
NODE_CLASSIFY = "classify"
NODE_HUMAN_GATE = "human_gate"
NODE_FINALIZE = "finalize"


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
    """
    graph = StateGraph(ReviewState)

    # --- Узлы. Имена ветвящихся узлов берём из router.NODE_*, чтобы
    #     условное ребро и add_node не разошлись. ---
    graph.add_node(NODE_INGEST, ingest)
    graph.add_node(NODE_CLASSIFY, classify)
    graph.add_node(NODE_RETRIEVE, retrieve)
    graph.add_node(NODE_GENERATE, generate)
    graph.add_node(NODE_SPAM_FILTER, spam_filter)
    graph.add_node(NODE_HUMAN_GATE, human_gate)
    graph.add_node(NODE_FINALIZE, finalize)

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