"""
FastAPI-сервис: вебхук Telegram и служебные пробы.

ПОЧЕМУ ВЕБХУК, А НЕ POLLING
---------------------------
Polling проще завести, но он требует ЖИВОГО процесса, который сам ходит
в Telegram. На VPS под Docker это лишний долгоживущий исходящий коннект
и отдельная модель отказа. Вебхук укладывается в ту же схему, что и любой
другой HTTP-сервис за reverse-proxy: контейнер слушает порт, Telegram
стучится снаружи.

ЧТО ТРЕБУЕТСЯ СНАРУЖИ
---------------------
Telegram принимает ТОЛЬКО https и только с валидным сертификатом.
Значит, на VPS нужен reverse-proxy (nginx или Caddy) с Let's Encrypt,
проксирующий TELEGRAM_WEBHOOK_PATH на webhook_port контейнера.
Самоподписанный сертификат Telegram формально поддерживает, но это
лишняя нестандартная ветка — не стоит того.

ПОЧЕМУ ОЧЕРЕДЬ, А НЕ ОБРАБОТКА В ЗАПРОСЕ
----------------------------------------
Апдейт кладётся в application.update_queue, и эндпоинт немедленно
отвечает 200. Если обрабатывать внутри запроса, Telegram будет ждать
ответа всё время похода в Postgres — а не дождавшись, ПОВТОРИТ доставку.
Получили бы ретраи ровно на медленных операциях, то есть именно там,
где они опаснее всего. Идемпотентность у нас есть (повторный resume
упирается в ThreadNotPaused), но полагаться на неё вместо быстрого 200
неправильно.

ПРОВЕРКА СЕКРЕТА
----------------
Эндпоинт по определению открыт в интернет. Единственное, что отличает
Telegram от постороннего, — заголовок X-Telegram-Bot-Api-Secret-Token,
который Telegram присылает потому, что мы задали secret_token в setWebhook.
Сравнение через hmac.compare_digest, а не '==': сравнение секретов
обычным оператором утекает по времени.

Запуск:
    python -m scripts.run_webhook
    # или напрямую:
    uvicorn reviews_agent.api.service:app --host 0.0.0.0 --port 8080
"""

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application

from reviews_agent.config import get_settings
from reviews_agent.tg.handlers import register_handlers

logger = logging.getLogger(__name__)

settings = get_settings()

# Апдейты, которые нам нужны. Всё остальное Telegram даже не пришлёт —
# меньше мусора в очереди и меньше поверхности для сюрпризов.
ALLOWED_UPDATES = ["message", "callback_query"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Поднимает приложение Telegram и регистрирует вебхук.

    .updater(None) обязателен: без него PTB собирает Updater для polling,
    который в вебхук-режиме не нужен и будет мешать.

    drop_pending_updates=True при старте — осознанно. Апдейты, накопившиеся
    пока сервис лежал, относятся к карточкам, чьё состояние в графе могло
    измениться. Разгребать их вслепую хуже, чем отбросить: оператор может
    запросить карточку заново через scripts/run_graph.py --card.
    """
    tg_app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .updater(None)
        .build()
    )
    register_handlers(tg_app)

    await tg_app.initialize()
    await tg_app.start()

    webhook_url = settings.telegram_webhook_url
    if webhook_url:
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        logger.info("Вебхук зарегистрирован: %s", webhook_url)
    else:
        logger.warning(
            "TELEGRAM_WEBHOOK_BASE_URL пуст — setWebhook не вызывался. "
            "Сервис поднят, но Telegram в него не стучится."
        )

    app.state.tg_app = tg_app
    try:
        yield
    finally:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("Приложение Telegram остановлено.")


app = FastAPI(title="reviews-agent HITL webhook", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Проба для proxy/оркестратора. Базу не трогает — это проверка процесса, не стека."""
    return {"status": "ok", "webhook": bool(settings.telegram_webhook_url)}


@app.post(settings.telegram_webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Принимает апдейт, проверяет секрет, кладёт в очередь и сразу отвечает 200."""
    expected = settings.telegram_webhook_secret.get_secret_value()
    if expected:
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, expected
        ):
            logger.warning("Отклонён запрос вебхука с неверным secret_token.")
            raise HTTPException(status_code=403, detail="forbidden")

    tg_app: Application = request.app.state.tg_app
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)

    await tg_app.update_queue.put(update)
    return {"ok": True}