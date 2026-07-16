"""
Состояние графа LangGraph.

Единый словарь, который течёт через все узлы: каждый узел читает нужные ему
поля и возвращает только свои. LangGraph мержит возвращённое в состояние.

ВАЖНО ПРО ТИПЫ: состояние сериализуется в чекпоинты Postgres через msgpack
в строгом режиме (LANGGRAPH_STRICT_MSGPACK=true). Поэтому здесь живут только
простые типы — str, int, float, bool, None, list, dict. Никаких numpy-массивов,
соединений psycopg или объектов модели: вектор запроса считается внутри узла
Retrieve и там же умирает, а в состояние попадают лишь найденные тексты и скоры.
"""

import operator
from typing import Annotated, Literal, TypedDict

# Литералы дублируют CHECK-констрейнты таблицы, значения в _expected фикстур
# и допустимые ответы классификатора. Одно место истины на весь проект.
Language = Literal["uk", "en"]
Category = Literal["complaint", "question", "praise", "spam"]
Sentiment = Literal["positive", "negative", "neutral"]
Urgency = Literal["low", "medium", "high"]
ReviewStatus = Literal["pending", "approved", "rejected", "edited", "skipped"]


class KnowledgeChunk(TypedDict):
    """
    Чанк базы знаний, попавший в контекст генерации.

    ДВА СКОРА В РАЗНЫХ ШКАЛАХ — путать нельзя:

    vector_score — косинусная схожесть e5, диапазон 0..1 (на практике 0.78-0.88).
        Первая ступень поиска: грубое сито, отбирает кандидатов из базы.

    rerank_score — логит cross-encoder (bge-reranker-base), обычно от -11 до 0,
        может быть и положительным. Вторая ступень: решает, попадёт ли чанк
        в контекст. Именно по нему работает порог.

    Замер показал, почему нужны обе ступени: на векторе нужный чанк для rev_003
    набирал 0.8368, а шум для rev_004 — 0.8408, то есть шум был ВЫШЕ нужного
    и порога не существовало. Реранкер перевернул порядок: -2.139 против -2.970.

    У статического контекста (голос бренда, примеры ответов) оба поля None:
    он берётся прямой выборкой по type, не ранжируется и скоров не имеет.
    Придумывать ему единицу нельзя — фальшивое число всплывёт в отладке.
    """

    content: str
    type: str
    language: str
    source: str
    vector_score: float | None
    rerank_score: float | None


class ProcessingError(TypedDict):
    """Техническая ошибка на узле — для алерта админу и разбора."""

    node: str
    message: str
    timestamp: str


class ReviewState(TypedDict, total=False):
    """
    Состояние обработки одного отзыва.

    total=False: узлы возвращают только свои поля, а не весь словарь целиком.
    """

    # --- Вход: приходит из фикстуры (позже — из интеграции) ---
    review_id: str
    client_id: str
    source: str
    author: str
    rating: int
    created_at: str
    text: str

    # --- Ingest & detect ---
    language: Language
    cyrillic_ratio: float  # доля кириллицы — для лога и отладки детекта

    # --- Classify (Haiku): семантика, которую решает модель ---
    category: Category
    sentiment: Sentiment
    urgency: Urgency

    # --- Эскалация ---
    escalate: bool
    escalation_reason: str  # почему подняли флаг — попадёт в карточку Telegram

    # --- Retrieve: факты под конкретный отзыв ---
    # Чанки faq/policy, прошедшие вектор и реранкер. Список может быть ПУСТЫМ —
    # и для жалобы это не сбой, а сигнал: решения в базе знаний нет,
    # отзыв идёт на эскалацию.
    context_chunks: list[KnowledgeChunk]

    # --- Generate: статический контекст ---
    # Голос бренда и примеры ответов. Заполняется в Generate, а не в Retrieve:
    # это конфигурация клиента, а не результат поиска. Ветка praise минует
    # Retrieve, но контекст бренда получает — иначе ответ на подяку писался бы
    # без правил тона.
    # В состоянии лежит ради трассировки: по чекпоинту видно, на чём именно
    # построен ответ.
    brand_context: list[KnowledgeChunk]

    # --- Generate: результат ---
    draft: str
    guardrail_flags: list[str]  # что не так с черновиком — флаги, не блокировка

    # --- HITL ---
    review_status: ReviewStatus
    final_reply: str

    # --- Error handling ---
    # Annotated с operator.add — reducer: ошибки НАКАПЛИВАЮТСЯ, а не
    # перезаписываются. Без него ошибка второго узла стёрла бы ошибку первого,
    # и в алерт ушла бы неполная картина.
    # Поле обязано быть инициализировано пустым списком при запуске графа —
    # reducer применяется к существующему значению.
    errors: Annotated[list[ProcessingError], operator.add]