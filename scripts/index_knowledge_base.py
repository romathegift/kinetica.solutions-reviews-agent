"""
Индексация базы знаний: data/knowledge_base.json -> Postgres (pgvector).

Запуск из корня проекта при поднятом SSH-туннеле:
    python -m scripts.index_knowledge_base

Переиндексация полная: TRUNCATE + INSERT. Для 22 чанков это дешевле
и надёжнее upsert — база всегда ровно соответствует JSON, без дрейфа.
"""

import json
import sys

from psycopg.rows import dict_row

from reviews_agent.config import PROJECT_ROOT
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
    # 1. Проверяем базу ДО векторизации: обидно ждать модель,
    #    чтобы упасть на отсутствующей таблице.
    env = check_connection()
    print(
        f"База: {env['database']} | пользователь: {env['user']} "
        f"| vector: {env['vector_extension']}"
    )

    if env["vector_extension"] is None:
        sys.exit("Расширение vector не активно в базе.")
    if not env["knowledge_base_exists"]:
        sys.exit("Таблицы knowledge_base нет — индексировать некуда.")

    print(f"В таблице сейчас чанков: {env['knowledge_base_rows']}")

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

    # 4. Запись. TRUNCATE и INSERT в одной транзакции: если вставка упадёт,
    #    старые данные вернутся, база не останется пустой.
    #    RESTART IDENTITY сбрасывает счётчик BIGSERIAL — id снова с 1.
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("TRUNCATE knowledge_base RESTART IDENTITY")

            cur.executemany(
                """
                INSERT INTO knowledge_base
                    (content, embedding, type, language, category, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
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
            cur.execute("SELECT count(*) AS n FROM knowledge_base")
            total = cur.fetchone()["n"]

            cur.execute(
                "SELECT count(*) AS n FROM knowledge_base WHERE embedding IS NULL"
            )
            null_vectors = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT type, language, count(*) AS n
                FROM knowledge_base
                GROUP BY type, language
                ORDER BY type, language
                """
            )
            breakdown = cur.fetchall()

    print(f"\nВставлено чанков: {total}")
    print(f"Чанков без вектора: {null_vectors}")
    print("Раскладка:")
    for row in breakdown:
        print(f"  {row['type']:<8} {row['language']:<3} {row['n']}")


if __name__ == "__main__":
    try:
        index()
    finally:
        # Пул закрываем всегда — иначе процесс может зависнуть,
        # удерживая соединения через SSH-туннель.
        close_pool()