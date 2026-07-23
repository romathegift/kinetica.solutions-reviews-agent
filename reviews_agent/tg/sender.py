"""
Отправка карточки оператору.

ПОЧЕМУ ОТПРАВКА НЕ ЖИВЁТ В УЗЛЕ human_gate
------------------------------------------
Это главное решение шага R1, и оно неочевидно.

При Command(resume=...) LangGraph НЕ продолжает узел с того места, где
стоял interrupt(). Он ПЕРЕИСПОЛНЯЕТ узел с начала, и interrupt() на этот
раз возвращает переданное решение вместо того, чтобы прерывать. Значит,
любой побочный эффект, написанный в узле ДО interrupt(), выполняется
повторно на каждом возобновлении.

Положи send_message в human_gate — и оператор получит вторую карточку
ровно в тот момент, когда нажал кнопку на первой. Ошибка тихая: граф
отработает верно, состояние будет верным, продублируется только то, что
видит человек.

Поэтому отправка живёт там, где читается __interrupt__: в вызывающем коде
(scripts/run_graph.py) — и никогда внутри узла.

СИНХРОННАЯ ОБЁРТКА
------------------
python-telegram-bot асинхронный, а CLI-скрипт синхронный. send_operator_card
делает asyncio.run для CLI; из уже работающего event loop (вебхук) нужно
звать send_operator_card_async напрямую. Обёртка это проверяет и падает
внятно, а не выдаёт «asyncio.run() cannot be called from a running event
loop» посреди обработчика.
"""

import asyncio

from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode

from reviews_agent.config import get_settings
from reviews_agent.tg.card import build_card_text, build_keyboard

# Превью ссылок в карточке не нужно: отзыв клиента может содержать ссылку,
# и раскрытая карточка чужого сайта займёт пол-экрана над черновиком.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_operator_card_async(payload: dict, bot: Bot | None = None) -> int:
    """
    Отправляет карточку и возвращает message_id.

    bot передаётся, когда вызов идёт изнутри уже поднятого приложения
    (context.bot): переиспользовать его дешевле, чем открывать второй
    HTTP-клиент. Без bot создаётся свой, короткоживущий — это путь CLI.

    message_id возвращается, потому что он нужен, чтобы позже снять
    с карточки кнопки: без него принятое решение останется с активными
    кнопками, и второе нажатие выглядело бы для оператора допустимым.
    """
    settings = get_settings()
    text = build_card_text(payload)
    keyboard = build_keyboard(payload["review_id"])
    chat_id = settings.telegram_chat_id

    if bot is not None:
        message = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=NO_PREVIEW,
        )
        return message.message_id

    async with Bot(settings.telegram_bot_token.get_secret_value()) as own_bot:
        message = await own_bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=NO_PREVIEW,
        )
        return message.message_id


def send_operator_card(payload: dict) -> int:
    """
    Синхронная обёртка для CLI.

    Из работающего event loop бросает RuntimeError с указанием на
    асинхронный вариант: неявно уйти в другой поток здесь хуже,
    чем упасть с понятным текстом.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(send_operator_card_async(payload))

    raise RuntimeError(
        "send_operator_card вызван из работающего event loop. "
        "Внутри async-кода зови send_operator_card_async(payload, bot=...)."
    )