"""
Точка входа вебхук-сервиса для ЛОКАЛЬНОЙ разработки.

    python -m scripts.run_webhook

Отдельный скрипт, а не голый uvicorn в командной строке, ради одного:
перед стартом печатается, КУДА зарегистрируется вебхук и слушает ли
сервис ожидаемый порт. Половина проблем с вебхуком — это несовпадение
пути в proxy и пути в setWebhook, и увидеть их обе рядом дешевле, чем
искать в логах Telegram.

В Docker (R3) точка входа — голый uvicorn, этот скрипт там не
выполняется. Именно поэтому настройка логирования БОЛЬШЕ НЕ ЖИВЁТ ЗДЕСЬ:
она переехала в reviews_agent/logging_setup.py и вызывается из lifespan
приложения, то есть при любом способе запуска. Здесь она вызывается лишь
для того, чтобы баннер ниже печатался в уже настроенное логирование.
"""

import logging

import uvicorn

from reviews_agent.config import get_settings
from reviews_agent.logging_setup import (
    configure_logging,
    mute_token_leaking_loggers,  # noqa: F401  — сохранено как публичное имя
)


def main() -> None:
    configure_logging(level=logging.INFO)

    settings = get_settings()

    secret_state = (
        "задан"
        if settings.telegram_webhook_secret.get_secret_value()
        else "ПУСТ (проверка отключена!)"
    )

    print("=" * 78)
    print("ВЕБХУК HITL-ГЕЙТА")
    print("=" * 78)
    print(f"  слушаем:        http://{settings.webhook_host}:{settings.webhook_port}")
    print(f"  путь вебхука:   {settings.telegram_webhook_path}")
    print(f"  публичный URL:  {settings.telegram_webhook_url or '(не задан — setWebhook не будет)'}")
    print(f"  чат оператора:  {settings.telegram_chat_id}")
    print(f"  secret_token:   {secret_state}")
    print(f"  логи httpx:     WARNING (URL с токеном не печатается)")
    print("=" * 78)

    uvicorn.run(
        "reviews_agent.api.service:app",
        host=settings.webhook_host,
        port=settings.webhook_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()