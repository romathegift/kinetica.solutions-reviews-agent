"""
Обработчики Telegram: нажатие кнопки и присланная правка.

ЧТО ЗДЕСЬ ВАЖНО, КРОМЕ ОЧЕВИДНОГО
---------------------------------
1. answer() ВСЕГДА и БЫСТРО. Telegram ждёт ответа на callback_query
   считанные секунды; пока его нет, на кнопке крутится часик, и оператор
   жмёт второй раз. Поэтому answer() уходит ДО обращения к базе.

2. resume — синхронный код, идущий в Postgres. Внутри event loop его
   нельзя звать напрямую: он заблокирует обработку остальных апдейтов
   на всё время запроса. asyncio.to_thread уносит его в пул потоков.

3. ThreadNotPaused — НОРМАЛЬНЫЙ исход, а не сбой. Telegram ретраит
   доставку апдейтов, а оператор может нажать дважды. Обе ситуации дают
   один и тот же путь: сказать «уже обработано», снять кнопки, граф
   не трогать. Единственная защита от двойного применения решения —
   само состояние нити в Postgres, и она достаточна: повторный resume
   физически не может пройти, потому что паузы больше нет.

4. КАРТОЧКА СТАРШЕ ~48 ЧАСОВ. Telegram не отдаёт callback_query.message
   для старых сообщений. Для этого продукта это штатный сценарий, а не
   край: пауза HITL по замыслу живёт часами и днями. Решение применяется
   в любом случае — review_id лежит в callback_data. Теряется только
   возможность снять кнопки и ответить в нить карточки; тогда итог
   уходит отдельным сообщением. Отказаться обрабатывать было бы хуже
   всего: отзыв завис бы навсегда именно потому, что оператор был занят.

5. Проверка chat_id. Бот отвечает только в тот чат, что указан в
   настройках. Токен бота может утечь, а решение оператора публикует
   текст от имени бренда.
"""

import asyncio
import logging

from telegram import ForceReply, ReplyParameters, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from reviews_agent.config import get_settings
from reviews_agent.hitl import (
    HitlError,
    ThreadNotFound,
    ThreadNotPaused,
    decision_approve,
    decision_edit,
    decision_reject,
    resume_thread,
)
from reviews_agent.tg.card import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_REJECT,
    CALLBACK_PATTERN,
    NO_CARD,
    build_already_done_text,
    build_edit_prompt,
    build_result_text,
    parse_callback_data,
    parse_edit_marker,
)

logger = logging.getLogger(__name__)


# ======================================================================
# Вспомогательное
# ======================================================================
def _is_operator_chat(update: Update) -> bool:
    """Разрешён только настроенный чат оператора."""
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.id == get_settings().telegram_chat_id


async def _clear_keyboard(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, card_message_id: int
) -> None:
    """
    Снимает кнопки с карточки.

    NO_CARD — нечего снимать (сообщение старше 48 часов). Ошибку Telegram
    глушим намеренно: карточку могли удалить или отредактировать вручную.
    Ни один из этих случаев не должен помешать доложить оператору результат
    — решение уже применено к графу, и оно важнее внешнего вида сообщения.
    """
    if card_message_id == NO_CARD:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=card_message_id, reply_markup=None
        )
    except TelegramError as exc:
        logger.warning("Не удалось снять кнопки с сообщения %s: %s", card_message_id, exc)


async def _report(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, card_message_id: int, text: str
) -> None:
    """
    Сообщает оператору исход — ответом на карточку, если она доступна.

    Без карточки (NO_CARD) уходит обычным сообщением: связь с отзывом
    несёт сам текст, в нём есть review_id.
    """
    reply_parameters = (
        None
        if card_message_id == NO_CARD
        else ReplyParameters(message_id=card_message_id, allow_sending_without_reply=True)
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_parameters=reply_parameters,
    )


async def _apply_decision(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    card_message_id: int,
    review_id: str,
    decision: dict,
) -> None:
    """
    Общий путь применения решения для всех трёх действий.

    Один путь на approve/reject/edit — потому что различие между ними
    целиком выражено содержимым decision. Развести их по трём веткам
    значило бы завести три места, где может разъехаться обработка
    ThreadNotPaused.
    """
    try:
        final_state = await asyncio.to_thread(resume_thread, review_id, decision)

    except ThreadNotPaused as exc:
        await _clear_keyboard(context, chat_id, card_message_id)
        await _report(
            context,
            chat_id,
            card_message_id,
            build_already_done_text(review_id, exc.review_status),
        )
        return

    except ThreadNotFound:
        await _clear_keyboard(context, chat_id, card_message_id)
        await _report(
            context,
            chat_id,
            card_message_id,
            f"⚠️ Нитку <b>{review_id}</b> не знайдено — її було стерто. "
            f"Рішення не застосовано.",
        )
        return

    except (HitlError, ValueError) as exc:
        logger.exception("Ошибка применения решения по %s", review_id)
        await _report(context, chat_id, card_message_id, f"❌ Помилка: {exc}")
        return

    await _clear_keyboard(context, chat_id, card_message_id)
    await _report(
        context,
        chat_id,
        card_message_id,
        build_result_text(
            review_id,
            final_state.get("review_status", ""),
            final_state.get("final_reply", ""),
        ),
    )


# ======================================================================
# Нажатие кнопки
# ======================================================================
async def on_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve / Reject применяются сразу; Edit переводит в режим ввода текста."""
    query = update.callback_query
    if query is None:
        return

    if not _is_operator_chat(update):
        await query.answer("Немає доступу.", show_alert=True)
        return

    parsed = parse_callback_data(query.data or "")
    if parsed is None:
        # Кнопка от старой версии карточки после деплоя — не ошибка бота.
        await query.answer("Кнопка застаріла. Запросіть картку заново.", show_alert=True)
        return

    action, review_id = parsed
    chat_id = update.effective_chat.id

    # Сообщения старше ~48 часов Telegram не отдаёт. Решение это не блокирует.
    card_message_id = query.message.message_id if query.message is not None else NO_CARD

    if action == ACTION_EDIT:
        await query.answer()
        reply_parameters = (
            None
            if card_message_id == NO_CARD
            else ReplyParameters(
                message_id=card_message_id, allow_sending_without_reply=True
            )
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=build_edit_prompt(review_id, card_message_id),
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(input_field_placeholder="Новий текст відповіді"),
            reply_parameters=reply_parameters,
        )
        return

    # answer() до похода в базу: иначе оператор видит зависшую кнопку.
    await query.answer("Обробляю…")

    decision = decision_approve() if action == ACTION_APPROVE else decision_reject()
    await _apply_decision(context, chat_id, card_message_id, review_id, decision)


# ======================================================================
# Присланная правка
# ======================================================================
async def on_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Текст, присланный ответом на сообщение-приглашение.

    review_id и id карточки читаются из процитированного текста, а не из
    памяти процесса: правка переживает рестарт сервиса.
    """
    message = update.effective_message
    if message is None or message.reply_to_message is None:
        return

    if not _is_operator_chat(update):
        return

    marker = parse_edit_marker(message.reply_to_message.text or "")
    if marker is None:
        # Оператор ответил на что-то другое — это не правка, молчим.
        return

    review_id, card_message_id = marker
    chat_id = update.effective_chat.id

    try:
        decision = decision_edit(message.text or "")
    except ValueError:
        await message.reply_text("⚠️ Текст правки порожній. Надішліть відповідь ще раз.")
        return

    await _apply_decision(context, chat_id, card_message_id, review_id, decision)


# ======================================================================
# Регистрация
# ======================================================================
def register_handlers(application: Application) -> None:
    """
    Подключает обработчики к приложению.

    MessageHandler ловит ТОЛЬКО текстовые ответы (filters.REPLY), чтобы
    обычные сообщения в чате не будили логику правки.
    """
    application.add_handler(CallbackQueryHandler(on_decision, pattern=CALLBACK_PATTERN))
    application.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, on_edit_reply)
    )