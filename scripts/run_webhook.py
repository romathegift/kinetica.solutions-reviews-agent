"""
Точка входа вебхук-сервиса.

    python -m scripts.run_webhook

Отдельный скрипт, а не голый uvicorn в командной строке, ради одного:
перед стартом печатается, КУДА зарегистрируется вебхук и слушает ли
сервис ожидаемый порт. Половина проблем с вебхуком — это несовпадение
пути в proxy и пути в setWebhook, и увидеть их обе рядом дешевле, чем
искать в логах Telegram.

В Docker (шаг R3) точкой входа станет uvicorn напрямую — там печатать
некому. ВАЖНО: глушение httpx (см. mute_token_leaking_loggers) должно
переехать туда же, иначе токен окажется в docker logs.
"""

import logging

import uvicorn

from reviews_agent.config import get_settings


# У Telegram Bot API токен зашит прямо в путь запроса:
#   https://api.telegram.org/bot<TOKEN>/sendMessage
# httpx на уровне INFO печатает полный URL каждого запроса, поэтому при
# basicConfig(level=INFO) токен уходит в stdout на КАЖДОМ обращении к API,
# а на R3 — ещё и в docker logs. WARNING оставляет видимыми ошибки
# транспорта, но убирает строку с URL.
TOKEN_LEAKING_LOGGERS = ("httpx", "httpcore", "telegram.request")


def mute_token_leaking_loggers() -> None:
    """Поднять до WARNING логгеры, которые печатают URL Telegram API."""
    for name in TOKEN_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    mute_token_leaking_loggers()

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