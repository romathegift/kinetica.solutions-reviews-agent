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

# Путь healthcheck-эндпоинта. Литерал, а не settings: этот модуль
# намеренно не зависит от конфига — логирование должно настраиваться
# раньше и надёжнее, чем читается .env.
HEALTH_PATH = "/health"

# Логгер uvicorn, пишущий строки доступа вида
# '127.0.0.1:56424 - "GET /health HTTP/1.1" 200 OK'
ACCESS_LOGGER = "uvicorn.access"


def mute_token_leaking_loggers() -> None:
    """Поднять до WARNING логгеры, которые печатают URL Telegram API."""
    for name in TOKEN_LEAKING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class HealthcheckAccessFilter(logging.Filter):
    """
    Убирает из access-лога УСПЕШНЫЕ обращения к healthcheck.

    Docker-compose дёргает /health каждые 30 секунд, и за сутки это
    несколько тысяч строк, среди которых тонут реальные события —
    вебхуки Telegram, ошибки, старт и остановка сервиса.

    Разбираем аргументы записи, а не готовый текст. uvicorn логирует
    строку доступа так:

        logger.info('%s - "%s %s HTTP/%s" %d',
                    client_addr, method, full_path, http_version, status_code)

    то есть args[2] — путь, args[4] — код ответа. Сравнение пути целиком
    вместо поиска подстроки: подстрока отбросила бы и '/health-report',
    и любой другой эндпоинт, начинающийся так же.

    Ответы не-200 НЕ глушатся. Падающий healthcheck — единственная
    причина, по которой этот эндпоинт вообще нужен в логах.
    """

    def __init__(self, path: str = HEALTH_PATH) -> None:
        super().__init__()
        self.path = path

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            # Не строка доступа uvicorn (или формат изменился) — пропускаем.
            return True

        path, status = args[2], args[4]

        # Путь может прийти с query-строкой: '/health?x=1'.
        path_only = str(path).split("?", 1)[0]

        return not (path_only == self.path and status == 200)


def silence_healthcheck_access_logs(path: str = HEALTH_PATH) -> None:
    """
    Повесить фильтр healthcheck на access-логгер uvicorn.

    Порядок важен и он в нашу пользу: uvicorn применяет свой
    LOGGING_CONFIG при старте сервера, ДО того как начнёт выполняться
    lifespan. dictConfig сбрасывает фильтры у логгеров, которые описывает,
    поэтому фильтр, навешенный из lifespan, ложится поверх и переживает
    настройку uvicorn — а не наоборот.

    Идемпотентна: повторный вызов не плодит дубликаты фильтров.
    """
    logger = logging.getLogger(ACCESS_LOGGER)

    for existing in logger.filters:
        if isinstance(existing, HealthcheckAccessFilter) and existing.path == path:
            return

    logger.addFilter(HealthcheckAccessFilter(path))


def configure_logging(level: int = logging.INFO) -> None:
    """
    Настроить корневой логгер, заглушить утечку токена и шум healthcheck.

    Вызывать при старте приложения ЛЮБЫМ способом запуска.

    Идемпотентна: basicConfig ничего не делает, если у корневого логгера
    уже есть хендлер, уровни логгеров выставляются заново (это дёшево),
    а фильтр access-логгера защищён от дублирования.

    Про uvicorn: его LOGGING_CONFIG применяется с disable_existing_loggers
    = False и не описывает root, поэтому и наш хендлер на корне, и уровень
    WARNING у httpx переживают его dictConfig.

    Отдельно про basicConfig: при запуске голым uvicorn его не вызывает
    никто, у корневого логгера нет хендлеров, и всё, что приложение пишет
    на INFO, просто исчезает. В docker logs это выглядит как «логов нет».
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    mute_token_leaking_loggers()
    silence_healthcheck_access_logs()