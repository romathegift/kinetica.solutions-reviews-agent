"""
Настройка логирования — на уровне ПРИЛОЖЕНИЯ, а не точки запуска.

ПОЧЕМУ ЗДЕСЬ, А НЕ В СКРИПТЕ ЗАПУСКА
------------------------------------
У Telegram Bot API токен зашит прямо в путь запроса:

    https://api.telegram.org/bot<TOKEN>/sendMessage

httpx на уровне INFO печатает полный URL КАЖДОГО запроса, то есть при
обычном basicConfig(level=INFO) токен уходит в stdout на каждом обращении
к API — а в контейнере ещё и в docker logs, которые переживают сам
контейнер.

Пока глушение жило в scripts/run_webhook.py, защита держалась на том,
что сервис запускают именно этим скриптом. В Docker точка входа — голый
uvicorn, скрипт не выполняется, и защита исчезала бы молча. Свойство
безопасности должно принадлежать приложению, а не способу его запуска,
поэтому оно переехало в пакет и вызывается из lifespan.

WARNING, а не CRITICAL: ошибки транспорта остаются видимыми, уходит
только строка с URL.
"""

import logging

# Логгеры, печатающие URL Telegram API вместе с токеном.
TOKEN_LEAKING_LOGGERS = ("httpx", "httpcore", "telegram.request")

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def mute_token_leaking_loggers() -> None:
    """Поднять до WARNING логгеры, которые печатают URL Telegram API."""
    for name in TOKEN_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(level: int = logging.INFO) -> None:
    """
    Настроить корневой логгер и заглушить утечку токена.

    Вызывать при старте приложения ЛЮБЫМ способом запуска.
    Идемпотентна: basicConfig ничего не делает, если у корневого логгера
    уже есть хендлер, а уровни логгеров выставляются заново — это дёшево.

    Про uvicorn: его LOGGING_CONFIG применяется с disable_existing_loggers
    = False и не описывает root, поэтому и наш хендлер на корне, и уровень
    WARNING у httpx переживают его dictConfig.

    Отдельно про basicConfig: при запуске голым uvicorn его не вызывает
    никто, у корневого логгера нет хендлеров, и всё, что приложение пишет
    на INFO, просто исчезает. В docker logs это выглядит как «логов нет».
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    mute_token_leaking_loggers()