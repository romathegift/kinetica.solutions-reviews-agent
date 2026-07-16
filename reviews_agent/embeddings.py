"""
Эмбеддинги через fastembed (intfloat/multilingual-e5-large, ONNX, CPU).

Модель асимметричная: запрос и документ кодируются по-разному.
Поэтому наружу торчат две функции — embed_query и embed_passages,
а не одна универсальная embed.
"""

from functools import lru_cache
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

MODEL_NAME = "intfloat/multilingual-e5-large"
EMBEDDING_DIM = 1024  # должно совпадать с vector(1024) в knowledge_base

# Префиксы e5. Обязательны для ЛЮБОГО языка, включая украинский:
# модель обучена с ними, и без префиксов качество поиска заметно падает.
# Асимметрия важна: вопрос гостя — это "query: ", чанк базы знаний — "passage: ".
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

# Кэш модели (~2.24 ГБ) — вне папки проекта.
# В /tmp класть нельзя: macOS его чистит, и модель будет качаться заново.
CACHE_DIR = Path.home() / ".cache" / "fastembed"


@lru_cache
def get_model() -> TextEmbedding:
    """
    Singleton модели.

    Инициализация — это загрузка ONNX-весов в память (секунды, а при первом
    запуске ещё и скачивание 2.24 ГБ). Создавать модель на каждый вызов
    означало бы платить это на каждом отзыве.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(model_name=MODEL_NAME, cache_dir=str(CACHE_DIR))


def embed_passages(texts: list[str]) -> list[np.ndarray]:
    """
    Векторизует чанки базы знаний (для индексации).

    Возвращает список numpy-массивов — pgvector принимает их напрямую,
    потому что register_vector повешен на соединения в db.py.
    """
    prefixed = [PASSAGE_PREFIX + text for text in texts]
    # .embed() отдаёт генератор — разворачиваем в список,
    # иначе вычисление отложится до итерации в неожиданном месте
    return list(get_model().embed(prefixed))


def embed_query(text: str) -> np.ndarray:
    """
    Векторизует поисковый запрос (текст отзыва в узле Retrieve).

    Отдельная функция, а не флаг в embed_passages: перепутанный префикс
    не вызовет ошибку, он просто тихо ухудшит выдачу — такое ловится долго.
    """
    return list(get_model().embed([QUERY_PREFIX + text]))[0]