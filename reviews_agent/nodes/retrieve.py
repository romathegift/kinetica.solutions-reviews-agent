"""
Узел Retrieve: двухступенчатый поиск фактов под конкретный отзыв.

СТУПЕНЬ 1 — вектор (e5-large): грубое сито. Отбирает VECTOR_CANDIDATES
кандидатов из базы по косинусной схожести. Задача — не потерять нужное,
поэтому порог низкий.

СТУПЕНЬ 2 — cross-encoder (bge-reranker-base): точное сито. Решает, какие
кандидаты реально отвечают на отзыв. Задача — не пропустить лишнее,
поэтому порог по категории (см. rerank.py).

Одной ступени не хватает: замер показал, что у вектора шум для rev_004
(0.8408) оказался ВЫШЕ нужного чанка для rev_003 (0.8368) — порядок неверный,
и никакой порог этого не чинит.

Ищет ТОЛЬКО факты (faq для вопросов, policy для жалоб). Голос бренда
и примеры ответов сюда не входят — это статический контекст, его берёт
Generate прямой выборкой.

Ветка praise этот узел минует: благодарности факты не нужны.
"""

from psycopg.rows import dict_row

from reviews_agent.db import get_connection
from reviews_agent.embeddings import embed_query
from reviews_agent.nodes.classify import compute_escalation
from reviews_agent.rerank import rerank_scores, threshold_for
from reviews_agent.state import KnowledgeChunk, ReviewState

# Сколько кандидатов отдаёт вектор реранкеру.
# Больше, чем итоговый TOP_K: реранкер должен иметь из чего выбирать.
# При 5 policy и 8 faq на клиента это почти вся выборка — и это нормально:
# лишние кандидаты стоят миллисекунды, а потерянный на первой ступени чанк
# вторая уже не вернёт.
VECTOR_CANDIDATES = 6

# Пол первой ступени. Отсекает только явный мусор: на замерах даже
# нерелевантные чанки набирают 0.78-0.80, а нужные — 0.82-0.87.
# Настоящий фильтр — на второй ступени.
VECTOR_FLOOR = 0.75

# Максимум чанков в контекст генерации после реранкера.
TOP_K = 4

# Какие типы чанков искать для какой категории отзыва.
# Сужает пул: для жалобы — 5 policy вместо 22 чанков, для вопроса — 8 faq.
# Без этого факты конкурировали бы за слоты с примерами и голосом бренда.
TYPES_BY_CATEGORY = {
    "complaint": ("policy",),
    "question": ("faq",),
}


def vector_search(
    query_text: str,
    client_id: str,
    category: str,
    limit: int = VECTOR_CANDIDATES,
    floor: float = VECTOR_FLOOR,
) -> list[KnowledgeChunk]:
    """
    Ступень 1: векторный поиск кандидатов.

    Фильтры в WHERE:
      client_id — изоляция клиентов, всегда;
      type      — faq или policy, по категории отзыва;
      category  — совпадает с категорией отзыва ИЛИ NULL (общие чанки).

    Язык НЕ фильтруем: e5 многоязычная, и запрос сам поднимает чанк своего
    языка выше дубля (замер: на английском запросе kb_002 en = 0.8513 против
    kb_001 uk = 0.8153). Жёсткий фильтр отрезал бы факт, который есть только
    на одном языке — например, kb_008 про животных существует лишь на украинском.
    """
    types = TYPES_BY_CATEGORY.get(category)
    if not types:
        # praise и spam сюда не попадают по маршруту роутера.
        # Если попали — это баг графа, и молчать о нём нельзя.
        raise ValueError(f"Retrieve не ищет факты для категории {category!r}")

    query_vector = embed_query(query_text)

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT
                    content,
                    type,
                    language,
                    source,
                    1 - (embedding <=> %s) AS score
                FROM knowledge_base
                WHERE client_id = %s
                  AND type = ANY(%s)
                  AND (category = %s OR category IS NULL)
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (query_vector, client_id, list(types), category, query_vector, limit),
            ).fetchall()

    return [
        KnowledgeChunk(
            content=row["content"],
            type=row["type"],
            language=row["language"],
            source=row["source"],
            vector_score=round(float(row["score"]), 4),
            rerank_score=None,  # проставит вторая ступень
        )
        for row in rows
        if row["score"] >= floor
    ]


def search_facts(
    query_text: str,
    client_id: str,
    category: str,
    top_k: int = TOP_K,
    threshold: float | None = None,
) -> list[KnowledgeChunk]:
    """
    Полный двухступенчатый поиск: вектор -> реранкер -> порог -> top_k.

    threshold=None означает "взять порог категории из rerank.py".
    Явное значение передаёт скрипт калибровки, чтобы прогнать сетку порогов
    без изменения кода.

    Отдельная функция, а не только узел: калибровка вызывает её напрямую,
    без запуска графа.
    """
    candidates = vector_search(query_text, client_id, category)
    if not candidates:
        return []

    # Ступень 2: реранкер. Скоры возвращаются в порядке входа —
    # сопоставляем с чанками по индексу.
    scores = rerank_scores(query_text, [c["content"] for c in candidates])
    for chunk, score in zip(candidates, scores):
        chunk["rerank_score"] = round(score, 4)

    limit = threshold_for(category) if threshold is None else threshold

    # Сортировка по rerank_score, а не по vector_score: порядок первой ступени
    # признан ненадёжным, в этом и был смысл второй.
    passed = [c for c in candidates if c["rerank_score"] >= limit]
    passed.sort(key=lambda c: c["rerank_score"], reverse=True)

    return passed[:top_k]


def retrieve(state: ReviewState) -> ReviewState:
    """
    Третий узел графа (для веток complaint и question): поиск фактов
    и пересчёт эскалации.

    Ищем по тексту отзыва целиком, а не по выжимке: отзыв короткий,
    а лишний вызов LLM ради переформулировки запроса добавил бы задержку
    и точку отказа там, где e5 с реранкером справляются сами.
    """
    chunks = search_facts(
        query_text=state["text"],
        client_id=state["client_id"],
        category=state["category"],
    )

    # Пересчёт эскалации со вторым поводом из дизайна: жалоба прошла Retrieve,
    # но ничего не прошло порог — решения в базе знаний нет.
    #
    # has_context важен ТОЛЬКО для complaint: вопрос без ответа в FAQ — обычное
    # дело (оператор ответит сам), а жалоба без policy означает, что ситуация
    # не покрыта регламентом и её должен увидеть человек.
    has_context = bool(chunks) if state["category"] == "complaint" else None

    escalate, reason = compute_escalation(
        sentiment=state["sentiment"],
        urgency=state["urgency"],
        rating=state["rating"],
        has_context=has_context,
    )

    return {
        "context_chunks": chunks,
        "escalate": escalate,
        "escalation_reason": reason,
    }