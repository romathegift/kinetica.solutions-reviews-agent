"""
Работа с Postgres.

Пул соединений на весь процесс + регистрация типа vector,
чтобы эмбеддинги ездили в обе стороны без ручной сериализации.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from reviews_agent.config import get_settings

# Сколько ждать свободное соединение из пула.
#
# ПОЧЕМУ НЕ ДЕФОЛТНЫЕ 30 СЕКУНД (замер R2, 2026-07-24)
# ----------------------------------------------------
# Дефолт рассчитан на конкуренцию под нагрузкой: много воркеров, мало
# коннектов, ждать выгоднее, чем падать. У нас противоположный профиль —
# отзывы идут единицами в час при max_size=5, за коннект конкурировать
# некому. Значит эти 30 секунд тратятся не на ожидание очереди, а только
# на недоступную базу: упавший туннель, перезапуск контейнера.
#
# Цена была измерена: при недоступном Postgres узел retrieve отдавал
# управление обработчику отказа через 97 секунд (три попытки графа
# по 30 секунд). Оператор столько ждать уведомления не должен.
#
# 10 секунд с запасом переживают моргание SSH-туннеля (переподключение
# занимает единицы секунд), но не превращают отказ в полторы минуты.
POOL_TIMEOUT = 10.0


def _configure_connection(conn: psycopg.Connection) -> None:
    """
    Callback пула: вызывается для КАЖДОГО нового соединения.

    register_vector работает на уровне соединения, а не глобально.
    Если зарегистрировать тип один раз при старте процесса, соединения,
    которые пул создаст позже, про vector знать не будут — и запись
    эмбеддинга упадёт с ошибкой типа.
    """
    register_vector(conn)


@lru_cache
def get_pool() -> ConnectionPool:
    """
    Singleton пула соединений.

    Пул переживает отдельные запросы: открыть соединение к Postgres
    через SSH-туннель дорого, а узлам графа коннект нужен часто.

    ВАЖНО ПРО РЕТРАИ: пул переподключается САМ, внутри POOL_TIMEOUT,
    и только исчерпав его бросает PoolTimeout. Это первый слой повторов
    для базы — графовая RetryPolicy в graph.py надстраивается над ним
    и намеренно держит малое число попыток, иначе таймауты умножаются.
    """
    settings = get_settings()

    pool = ConnectionPool(
        conninfo=settings.postgres_dsn,
        min_size=1,
        max_size=5,  # демо на 7 фикстурах — больше не нужно
        timeout=POOL_TIMEOUT,
        max_idle=300.0,  # простаивающие сверх min_size закрываются через 5 минут
        configure=_configure_connection,
        open=False,  # открываем явно ниже: неявное открытие в конструкторе устарело
    )
    pool.open()
    return pool


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """
    Соединение из пула как контекст-менеджер.

    Использование:
        with get_connection() as conn:
            rows = conn.execute("select 1").fetchall()

    На выходе из блока psycopg сам делает commit (или rollback при исключении)
    и возвращает соединение в пул.
    """
    with get_pool().connection() as conn:
        yield conn


def check_connection() -> dict[str, object]:
    """
    Диагностика окружения: база, пользователь, расширение vector, таблица knowledge_base.

    Вызывается на старте приложения и из скриптов — чтобы упасть с внятной
    ошибкой сразу, а не посреди индексации или прогона демо.
    """
    with get_connection() as conn:
        database, user = conn.execute(
            "select current_database(), current_user"
        ).fetchone()

        vector_row = conn.execute(
            "select extversion from pg_extension where extname = %s",
            ("vector",),
        ).fetchone()

        table_exists = conn.execute(
            "select count(*) from information_schema.tables where table_name = %s",
            ("knowledge_base",),
        ).fetchone()[0]

        # Считаем чанки только если таблица есть — иначе запрос упадёт
        kb_rows = None
        if table_exists:
            kb_rows = conn.execute("select count(*) from knowledge_base").fetchone()[0]

    return {
        "database": database,
        "user": user,
        "vector_extension": vector_row[0] if vector_row else None,
        "knowledge_base_exists": bool(table_exists),
        "knowledge_base_rows": kb_rows,
    }


def close_pool() -> None:
    """Закрыть пул — в тестах и при остановке приложения."""
    if get_pool.cache_info().currsize:
        get_pool().close()
        get_pool.cache_clear()