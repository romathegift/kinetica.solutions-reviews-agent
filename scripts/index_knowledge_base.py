"""
Индексация базы знаний: data/knowledge_base.json -> Postgres (pgvector).

Запуск из корня проекта при поднятом SSH-туннеле:
    python -m scripts.index_knowledge_base

Переиндексация полная, но в пределах одного клиента: DELETE по client_id + INSERT.
Для 22 чанков это дешевле и надёжнее upsert — база всегда ровно
соответствует JSON, без дрейфа.

ПОСЛЕ ПЕРЕИНДЕКСАЦИИ НУЖЕН РЕСТАРТ КОНТЕЙНЕРА
----------------------------------------------
generate() кэширует голос бренда и примеры ответов в памяти процесса
(reviews_agent/nodes/generate.py, раздел КЭШ СТАТИКИ). Этот скрипт запускается
ОТДЕЛЬНЫМ процессом (`docker exec ... scripts.index_knowledge_base`), и uvicorn
о переиндексации не узнаёт никак — у него свой процесс и своя память.
Без `docker compose restart reviews-agent` сервис продолжит отвечать старым
голосом бренда неопределённо долго. Напоминание печатается в конце index().
"""

import json
import sys

from psycopg.rows import dict_row

from reviews_agent.config import PROJECT_ROOT, get_settings
from reviews_agent.db import check_connection, close_pool, get_connection
from reviews_agent.embeddings import EMBEDDING_DIM, embed_passages

KB_FILE = PROJECT_ROOT / "data" / "knowledge_base.json"

# Допустимые значения дублируют CHECK-констрейнты таблицы.
# Проверяем до вставки, чтобы упасть с указанием конкретного id чанка,
# а не с сообщением Postgres про нарушение констрейнта без контекста.
VALID_TYPES = {"faq", "policy", "voice", "example"}
VALID_LANGUAGES = {"uk", "en"}
VALID_CATEGORIES = {"complaint", "question", "praise", "spam", None}


def load_chunks() -> list[dict]:
    """Читает JSON и валидирует каждый чанк против схемы таблицы."""
    with KB_FILE.open(encoding="utf-8") as f:
        chunks = json.load(f)

    errors: list[str] = []
    seen_ids: set[str] = set()

    for chunk in chunks:
        chunk_id = chunk.get("id", "<без id>")

        if chunk_id in seen_ids:
            errors.append(f"{chunk_id}: дубль id")
        seen_ids.add(chunk_id)

        if not chunk.get("content", "").strip():
            errors.append(f"{chunk_id}: пустой content")
        if chunk.get("type") not in VALID_TYPES:
            errors.append(f"{chunk_id}: недопустимый type={chunk.get('type')!r}")
        if chunk.get("language") not in VALID_LANGUAGES:
            errors.append(f"{chunk_id}: недопустимый language={chunk.get('language')!r}")
        if chunk.get("category") not in VALID_CATEGORIES:
            errors.append(f"{chunk_id}: недопустимый category={chunk.get('category')!r}")

    if errors:
        raise ValueError("Ошибки в knowledge_base.json:\n  " + "\n  ".join(errors))

    return chunks


def index() -> None:
    """Сценарий: проверка окружения -> чтение JSON -> векторизация -> запись."""
    client_id = get_settings().client_id

    # 1. Проверяем базу ДО векторизации: обидно ждать модель,
    #    чтобы упасть на отсутствующей таблице.
    env = check_connection()
    print(
        f"База: {env['database']} | пользователь: {env['user']} "
        f"| vector: {env['vector_extension']}"
    )
    print(f"Клиент: {client_id}")

    if env["vector_extension"] is None:
        sys.exit("Расширение vector не активно в базе.")
    if not env["knowledge_base_exists"]:
        sys.exit("Таблицы knowledge_base нет — индексировать некуда.")

    print(f"Всего чанков в таблице (все клиенты): {env['knowledge_base_rows']}")

    # 2. Читаем и валидируем JSON
    chunks = load_chunks()
    print(f"Прочитано чанков из JSON: {len(chunks)}")

    # 3. Векторизация одним батчем: fastembed на пачке эффективнее,
    #    чем на 22 отдельных вызовах.
    print("Векторизация...")
    vectors = embed_passages([chunk["content"] for chunk in chunks])

    # Страховка на случай смены модели: несовпадение размерности
    # иначе всплывёт как невнятная ошибка Postgres на вставке.
    for chunk, vector in zip(chunks, vectors):
        if vector.shape[0] != EMBEDDING_DIM:
            sys.exit(
                f"{chunk['id']}: размерность {vector.shape[0]}, "
                f"ожидалась {EMBEDDING_DIM}"
            )

    print(f"Векторов: {len(vectors)} | размерность: {vectors[0].shape[0]}")

    # 4. Запись. DELETE и INSERT в одной транзакции: если вставка упадёт,
    #    старые данные вернутся, база не останется пустой.
    #
    #    DELETE по client_id, а НЕ TRUNCATE: таблица общая для всех клиентов,
    #    и TRUNCATE снёс бы чужие базы знаний. Побочный эффект — счётчик
    #    BIGSERIAL не сбрасывается, id растут сквозным образом. Так и надо:
    #    стабильные идентификаторы чанков (kb_001...) живут в JSON, а не в базе.
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "DELETE FROM knowledge_base WHERE client_id = %s", (client_id,)
            )
            deleted = cur.rowcount

            cur.executemany(
                """
                INSERT INTO knowledge_base
                    (client_id, content, embedding, type, language, category, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        client_id,
                        chunk["content"],
                        vector,  # numpy едет как vector: register_vector в db.py
                        chunk["type"],
                        chunk["language"],
                        chunk["category"],
                        chunk["source"],
                    )
                    for chunk, vector in zip(chunks, vectors)
                ],
            )

            # 5. Контрольные числа — внутри той же транзакции
            cur.execute(
                "SELECT count(*) AS n FROM knowledge_base WHERE client_id = %s",
                (client_id,),
            )
            total = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT count(*) AS n FROM knowledge_base
                WHERE client_id = %s AND embedding IS NULL
                """,
                (client_id,),
            )
            null_vectors = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT type, language, count(*) AS n
                FROM knowledge_base
                WHERE client_id = %s
                GROUP BY type, language
                ORDER BY type, language
                """,
                (client_id,),
            )
            breakdown = cur.fetchall()

            # Контроль изоляции: чанки других клиентов не пострадали
            cur.execute(
                """
                SELECT client_id, count(*) AS n
                FROM knowledge_base
                GROUP BY client_id
                ORDER BY client_id
                """
            )
            by_client = cur.fetchall()

    print(f"\nУдалено старых чанков клиента: {deleted}")
    print(f"Вставлено чанков: {total}")
    print(f"Чанков без вектора: {null_vectors}")
    print("Раскладка:")
    for row in breakdown:
        print(f"  {row['type']:<8} {row['language']:<3} {row['n']}")
    print("Всего в таблице по клиентам:")
    for row in by_client:
        print(f"  {row['client_id']:<16} {row['n']}")

    # ПОСЛЕ ЭТОГО МЕСТА generate() кэширует статику в памяти процесса на
    # всё время его жизни (см. nodes/generate.py, КЭШ СТАТИКИ). Uvicorn
    # о переиндексации не узнает никак — у него свой процесс и своя память.
    # Без рестарта сервис продолжит отвечать старым голосом бренда
    # неопределённо долго.
    print(
        "\n"
        "⚠️  ВАЖНО: если reviews-agent уже запущен, статика (голос бренда,\n"
        "    примеры ответов) закэширована в его памяти и переиндексацию\n"
        "    ВЫШЕ НЕ УВИДИТ, пока контейнер не перезапущен.\n"
        "    Выполни на VPS: docker compose restart reviews-agent"
    )


if __name__ == "__main__":
    try:
        index()
    finally:
        # Пул закрываем всегда — иначе процесс может зависнуть,
        # удерживая соединения через SSH-туннель.
        close_pool()