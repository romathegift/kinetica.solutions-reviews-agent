"""
Узел Classify: категория/тональность/срочность через Claude Haiku 4.5
плюс детерминированный расчёт флага эскалации.

Разделение ответственности:
- модель решает СЕМАНТИКУ (что это за отзыв) — тут нужен язык и смысл;
- код решает АРИФМЕТИКУ (эскалировать или нет) — тут нужна предсказуемость.
"""

import json
from functools import lru_cache

from anthropic import Anthropic

from reviews_agent.config import get_settings
from reviews_agent.prompts.classify import (
    ASSISTANT_PREFILL,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)
from reviews_agent.state import ReviewState

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 200  # ответ — короткий JSON, запас на всякий случай

# Значения дублируют литералы из state.py и списки в промпте.
# Проверяем ответ модели: LLM может вернуть валидный JSON с невалидным
# значением ("very_high", "mixed"), и такое лучше поймать здесь,
# чем на роутере или в Telegram-карточке.
VALID_CATEGORIES = {"complaint", "question", "praise", "spam"}
VALID_SENTIMENTS = {"positive", "negative", "neutral"}
VALID_URGENCIES = {"low", "medium", "high"}


@lru_cache
def get_client() -> Anthropic:
    """Singleton клиента Anthropic — переиспользует HTTP-соединения."""
    return Anthropic(api_key=get_settings().anthropic_api_key.get_secret_value())


def compute_escalation(
    sentiment: str, urgency: str, rating: int, has_context: bool | None = None
) -> tuple[bool, str]:
    """
    Считает флаг эскалации и причину.

    Эскалация — это БИЗНЕС-сигнал ("зовите человека срочно"), а не ошибка.
    Генерацию ответа она не блокирует: гость всё равно получит черновик,
    просто карточка в Telegram придёт с пометкой приоритета.

    Поводов два (дизайн):
      1. Правило: negative AND (rating <= 2 OR urgency == high).
      2. Пустой Retrieve на жалобе — решения в базе знаний нет.
         Этот повод проверяется ПОЗЖЕ, в узле Retrieve, поэтому здесь
         has_context=None по умолчанию: на этапе классификации контекста ещё нет.

    Возвращает (флаг, причина). Причина уходит в карточку Telegram —
    оператор должен видеть, ПОЧЕМУ отзыв помечен приоритетным.
    """
    reasons: list[str] = []

    if sentiment == "negative":
        if rating <= 2:
            reasons.append(f"негативний відгук з оцінкою {rating}")
        if urgency == "high":
            reasons.append("висока терміновість (гроші, здоров'я або загроза ескалації)")

    # has_context is False -> Retrieve отработал и ничего не нашёл.
    # has_context is None -> Retrieve ещё не отработал, повод не применим.
    if has_context is False:
        reasons.append("у базі знань немає рішення для цієї скарги")

    return bool(reasons), "; ".join(reasons)


def classify(state: ReviewState) -> ReviewState:
    """
    Второй узел графа: классификация отзыва и расчёт эскалации.

    Исключения наружу НЕ ловим: обработка ошибок (retry с backoff,
    error_handler на add_node, алерт админу) навешивается на уровне графа
    на шаге 9. Узел должен честно упасть, чтобы механизм отработал.
    """
    settings = get_settings()

    response = get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0,  # классификация — не творчество: нужна воспроизводимость
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    language=state["language"],
                    text=state["text"],
                ),
            },
            # Префилл: модель продолжит ответ с этого символа и не сможет
            # начать с преамбулы или markdown-фенса.
            {"role": "assistant", "content": ASSISTANT_PREFILL},
        ],
    )

    # Префилл в ответ модели НЕ включается — возвращаем скобку на место сами.
    raw = ASSISTANT_PREFILL + response.content[0].text

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Haiku вернул невалидный JSON: {raw!r}") from exc

    category = parsed.get("category")
    sentiment = parsed.get("sentiment")
    urgency = parsed.get("urgency")

    # Валидация значений: JSON может быть синтаксически верным,
    # но семантически мусорным.
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Недопустимая category: {category!r}")
    if sentiment not in VALID_SENTIMENTS:
        raise ValueError(f"Недопустимый sentiment: {sentiment!r}")
    if urgency not in VALID_URGENCIES:
        raise ValueError(f"Недопустимый urgency: {urgency!r}")

    # Эскалация по правилу. Второй повод (пустой Retrieve) добавится позже,
    # в узле Retrieve — он пересчитает флаг с has_context.
    escalate, reason = compute_escalation(
        sentiment=sentiment,
        urgency=urgency,
        rating=state["rating"],
    )

    return {
        "category": category,
        "sentiment": sentiment,
        "urgency": urgency,
        "escalate": escalate,
        "escalation_reason": reason,
        # client_id ставим здесь: дальше по графу Retrieve фильтрует по нему,
        # и в состоянии он должен быть гарантированно.
        "client_id": state.get("client_id") or settings.client_id,
    }