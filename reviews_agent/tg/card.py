"""
Карточка оператору: чистое построение текста, клавиатуры и служебных маркеров.

ПОЧЕМУ ЭТОТ МОДУЛЬ БЕЗ СЕТИ
---------------------------
Всё, что здесь есть, — функции dict -> str и str -> tuple. Ни бота, ни
базы, ни настроек. Значит, вся разметка карточки, разбор callback_data
и разбор маркера правки покрываются обычными юнит-тестами: без токена,
без сети, за миллисекунды. Отправка живёт в sender.py именно поэтому.

CALLBACK_DATA
-------------
Telegram ограничивает callback_data 64 БАЙТАМИ. Формат "hitl:approve:rev_003"
укладывается с запасом, но review_id приходит извне (в проде это id
площадки-источника, а не наша фикстура), поэтому лимит проверяется явно
и нарушение падает при СБОРКЕ карточки, а не молчаливым отказом Telegram.

МАРКЕР ПРАВКИ — ЭТО СОСТОЯНИЕ, ХРАНИМОЕ В САМОМ СООБЩЕНИИ
---------------------------------------------------------
Когда оператор жмёт «Правка», бот отвечает сообщением с ForceReply, в
тексте которого спрятан маркер "#edit:<review_id>:<card_message_id>".
Ответ оператора приходит как reply на это сообщение — и review_id
читается из процитированного текста.

Альтернатива (словарь «жду правку от chat_id X») потребовала бы
переживающего рестарт хранилища: вебхук-сервис перезапускается при
каждом деплое, и незавершённая правка молча ломалась бы. Здесь состояния
нет вообще — сообщение само себя описывает.

card_message_id == 0 в маркере — документированный признак «карточка
недоступна» (см. NO_CARD ниже).
"""

import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ----------------------------------------------------------------------
# callback_data
# ----------------------------------------------------------------------
CALLBACK_NAMESPACE = "hitl"

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_EDIT = "edit"
ACTIONS = (ACTION_APPROVE, ACTION_REJECT, ACTION_EDIT)

# Паттерн для CallbackQueryHandler: чужие кнопки в этот обработчик не попадут.
CALLBACK_PATTERN = rf"^{CALLBACK_NAMESPACE}:(?:{'|'.join(ACTIONS)}):"

# Жёсткий лимит Telegram Bot API на callback_data.
CALLBACK_DATA_LIMIT = 64

# ----------------------------------------------------------------------
# Маркер правки
# ----------------------------------------------------------------------
EDIT_MARKER_RE = re.compile(r"#edit:([^\s:]+):(\d+)")

# Сообщение карточки недоступно для редактирования.
#
# Telegram НЕ отдаёт callback_query.message, если сообщению больше ~48 часов.
# Для этого продукта это не редкий край: пауза HITL по замыслу живёт часами
# и днями, оператор вполне может вернуться к карточке в понедельник. Решение
# в таком случае всё равно применяется (review_id лежит в callback_data),
# теряется только возможность снять кнопки и ответить в нить карточки.
NO_CARD = 0

# ----------------------------------------------------------------------
# Ограничения на длину
# ----------------------------------------------------------------------
# Лимит сообщения Telegram — 4096 символов. Режем поля с запасом: карточка
# должна остаться читаемой, а не упереться в отказ отправки на длинном
# отзыве. Полный текст всегда доступен в базе.
MAX_REVIEW_CHARS = 900
MAX_DRAFT_CHARS = 2400
MAX_REASON_CHARS = 300

# ----------------------------------------------------------------------
# Подписи (интерфейс оператора — украинский: оператор это персонал клиента)
# ----------------------------------------------------------------------
CATEGORY_LABELS = {
    "complaint": "🔴 Скарга",
    "question": "🔵 Запитання",
    "praise": "🟢 Подяка",
    "spam": "⚪️ Спам",
}

SENTIMENT_LABELS = {
    "positive": "позитивна",
    "neutral": "нейтральна",
    "negative": "негативна",
}

URGENCY_LABELS = {
    "low": "низька",
    "medium": "середня",
    "high": "🔥 висока",
}

RESULT_LABELS = {
    "approved": "✅ Схвалено — опубліковано чернетку без змін",
    "rejected": "❌ Відхилено — публікації не буде",
    "edited": "✏️ Опубліковано з правками оператора",
    "skipped": "⚪️ Пропущено",
}


# ======================================================================
# callback_data
# ======================================================================
def build_callback_data(action: str, review_id: str) -> str:
    """
    Собирает callback_data кнопки и проверяет лимит Telegram.

    Нарушение лимита падает здесь, при сборке карточки. Если положиться
    на Telegram, отказ придёт при отправке и будет выглядеть как «бот
    сломался», а не как «этот review_id слишком длинный».
    """
    if action not in ACTIONS:
        raise ValueError(f"Неизвестное действие {action!r}. Допустимо: {ACTIONS}.")
    if ":" in review_id:
        raise ValueError(
            f"review_id {review_id!r} содержит ':' — разделитель callback_data. "
            f"Разбор станет неоднозначным."
        )

    data = f"{CALLBACK_NAMESPACE}:{action}:{review_id}"
    size = len(data.encode("utf-8"))
    if size > CALLBACK_DATA_LIMIT:
        raise ValueError(
            f"callback_data {size} байт при лимите {CALLBACK_DATA_LIMIT}: {data!r}."
        )
    return data


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """
    Разбирает callback_data обратно в (action, review_id).

    Возвращает None на любом непонятном вводе, а не бросает: в чат могут
    прилететь кнопки от старой версии карточки после деплоя, и обработчик
    должен ответить оператору внятно, а не упасть.
    """
    parts = (data or "").split(":", 2)
    if len(parts) != 3:
        return None
    namespace, action, review_id = parts
    if namespace != CALLBACK_NAMESPACE or action not in ACTIONS or not review_id:
        return None
    return action, review_id


# ======================================================================
# Маркер правки
# ======================================================================
def build_edit_prompt(review_id: str, card_message_id: int) -> str:
    """
    Текст сообщения-приглашения к правке (отправляется с ForceReply).

    card_message_id вшивается в маркер, чтобы после получения правки можно
    было снять кнопки с ИСХОДНОЙ карточки. Telegram отдаёт reply_to_message
    только на один уровень вложенности, поэтому дотянуться до карточки
    через цепочку ответов нельзя — её id нужно нести с собой.

    card_message_id == NO_CARD означает «карточка недоступна»; правка
    применится, но кнопки снять будет не с чего.
    """
    return (
        f"✏️ <b>Правка відповіді на {html.escape(review_id)}</b>\n\n"
        f"Надішліть новий текст відповіді <b>у відповідь на це повідомлення</b>.\n"
        f"Він буде опублікований замість чернетки.\n\n"
        f"<code>#edit:{html.escape(review_id)}:{card_message_id}</code>"
    )


def parse_edit_marker(text: str) -> tuple[str, int] | None:
    """
    Достаёт (review_id, card_message_id) из текста сообщения-приглашения.

    Возвращает None, если маркера нет: значит, оператор ответил на что-то
    другое, и это не правка.
    """
    match = EDIT_MARKER_RE.search(text or "")
    if match is None:
        return None
    return match.group(1), int(match.group(2))


# ======================================================================
# Текст карточки
# ======================================================================
def _clip(value: str, limit: int) -> str:
    """Обрезает по лимиту, помечая обрезку — оператор должен видеть, что текст неполный."""
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + " […]"


def _esc(value) -> str:
    """HTML-экранирование. Текст отзыва пишет посторонний человек — '<' в нём легален."""
    return html.escape(str(value if value is not None else ""))


def build_card_text(payload: dict) -> str:
    """
    Собирает тело карточки из полезной нагрузки interrupt().

    Порядок блоков подчинён одному: оператор должен принять решение, не
    прокручивая. Сначала то, что меняет решение (эскалация, флаги
    гардрейлов), потом исходный отзыв, потом черновик.

    Неизвестные значения category/sentiment/urgency печатаются как есть,
    а не подменяются прочерком: если классификатор однажды вернёт новую
    метку, оператор должен её увидеть, а не потерять.
    """
    review_id = payload.get("review_id", "?")
    category = payload.get("category") or "—"
    sentiment = payload.get("sentiment") or "—"
    urgency = payload.get("urgency") or "—"
    escalate = bool(payload.get("escalate"))
    reason = payload.get("escalation_reason") or ""
    review_text = payload.get("text") or ""
    draft = payload.get("draft") or ""
    flags = payload.get("guardrail_flags") or []

    lines: list[str] = []

    lines.append(f"<b>Відгук {_esc(review_id)}</b>")
    lines.append(
        f"{CATEGORY_LABELS.get(category, _esc(category))}"
        f" · тональність: {SENTIMENT_LABELS.get(sentiment, _esc(sentiment))}"
        f" · терміновість: {URGENCY_LABELS.get(urgency, _esc(urgency))}"
    )

    if escalate:
        lines.append("")
        lines.append("⚠️ <b>ПОТРІБНА ЕСКАЛАЦІЯ</b>")
        if reason:
            lines.append(f"<i>{_esc(_clip(reason, MAX_REASON_CHARS))}</i>")

    if flags:
        lines.append("")
        lines.append(f"🚩 <b>Гардрейли ({len(flags)})</b>")
        for flag in flags:
            lines.append(f"• <code>{_esc(flag)}</code>")

    lines.append("")
    lines.append("<b>Текст відгуку</b>")
    lines.append(f"<blockquote>{_esc(_clip(review_text, MAX_REVIEW_CHARS))}</blockquote>")

    lines.append("")
    lines.append("<b>Чернетка відповіді</b>")
    lines.append(f"<blockquote>{_esc(_clip(draft, MAX_DRAFT_CHARS))}</blockquote>")

    return "\n".join(lines)


def build_keyboard(review_id: str) -> InlineKeyboardMarkup:
    """
    Клавиатура решения.

    Схвалити и Відхилити в один ряд (взаимоисключающие исходы), Правка
    отдельной строкой: это не третий равноправный вариант, а переход
    в другой режим работы.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Схвалити", callback_data=build_callback_data(ACTION_APPROVE, review_id)
                ),
                InlineKeyboardButton(
                    "❌ Відхилити", callback_data=build_callback_data(ACTION_REJECT, review_id)
                ),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Правка", callback_data=build_callback_data(ACTION_EDIT, review_id)
                ),
            ],
        ]
    )


def build_result_text(review_id: str, review_status: str, final_reply: str = "") -> str:
    """Итоговое сообщение после решения — отправляется ответом на карточку."""
    label = RESULT_LABELS.get(review_status, f"Статус: {_esc(review_status)}")
    lines = [f"<b>{_esc(review_id)}</b> — {label}"]
    if final_reply:
        lines.append("")
        lines.append("<b>Опубліковано</b>")
        lines.append(f"<blockquote>{_esc(_clip(final_reply, MAX_DRAFT_CHARS))}</blockquote>")
    return "\n".join(lines)


def build_already_done_text(review_id: str, review_status) -> str:
    """Ответ на повторное нажатие: решение уже принято, повторно граф не трогаем."""
    label = RESULT_LABELS.get(review_status, f"статус {_esc(review_status)}")
    return (
        f"ℹ️ <b>{_esc(review_id)}</b> вже оброблено раніше — {label}.\n"
        f"Повторне рішення не застосовується."
    )