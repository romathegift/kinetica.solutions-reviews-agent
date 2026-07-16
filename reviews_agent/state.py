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


class RetrievedChunk(TypedDict):
    """Чанк базы знаний, найденный узлом Retrieve."""

    content: str
    type: str
    language: str
    source: str
    score: float  # косинусная схожесть 0..1, чем выше — тем ближе


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

    # --- Retrieve ---
    context_chunks: list[RetrievedChunk]

    # --- Generate ---
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