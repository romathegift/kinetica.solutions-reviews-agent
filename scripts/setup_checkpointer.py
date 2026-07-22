"""
Разовая инициализация таблиц чекпоинтера LangGraph в Postgres.

Запускать ОДИН РАЗ на свежей базе перед первым прогоном графа, с поднятым
SSH-туннелем к VPS:

    python -m scripts.setup_checkpointer

Что делает: PostgresSaver.setup() создаёт служебные таблицы LangGraph
(checkpoints, checkpoint_writes, checkpoint_blobs и таблицу версий миграций).
Идемпотентно — повторный запуск не ломает уже созданное, а лишь
доводит схему до текущей версии. Служебные таблицы независимы от knowledge_base
и других таблиц проекта; DELETE/index_knowledge_base их не касается.

Почему отдельным скриптом, а не в build_graph: setup() — DDL-миграция,
операция уровня развёртывания, а не рантайма. Прод-процесс не должен
пытаться мигрировать схему на каждом старте — это гонки и лишние права
у рантайм-роли. Здесь она вызвана явно и осознанно.
"""

from langgraph.checkpoint.postgres import PostgresSaver

from reviews_agent.config import get_settings


def main() -> None:
    settings = get_settings()

    # from_conn_string — контекст-менеджер: открывает коннект с нужным
    # PostgresSaver режимом (dict_row, autocommit), на выходе закрывает.
    with PostgresSaver.from_conn_string(settings.postgres_dsn) as checkpointer:
        checkpointer.setup()

    print("✓ Таблицы чекпоинтера LangGraph созданы (или уже актуальны).")


if __name__ == "__main__":
    main()