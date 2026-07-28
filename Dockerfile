# syntax=docker/dockerfile:1

# ==========================================================================
# Reviews Agent — образ вебхук-сервиса HITL-гейта.
#
# Сборка ДВУХСТАДИЙНАЯ, и это не украшение. Веса моделей занимают 3.2 ГБ:
# multilingual-e5-large (2.1 ГБ) + bge-reranker-base (1.1 ГБ). Если качать
# их в той же стадии, где лежит код, любая правка кода будет заново тянуть
# 3.2 ГБ. Отдельная стадия зависит только от версии fastembed и двух имён
# моделей — правки кода её не трогают.
#
# Модели ЗАПЕКАЮТСЯ в образ, а не монтируются volume'ом: сервис держит
# HITL-паузы часами, и рестарт не должен зависеть от доступности
# HuggingFace. 129 ГБ свободных на VPS это позволяют.
# ==========================================================================


# --------------------------------------------------------------------------
# Стадия 1: прогрев кэша моделей.
# --------------------------------------------------------------------------
FROM python:3.11-slim AS models

# Путь кэша в коде задан как Path.home()/".cache"/"fastembed" и не
# конфигурируется. Значит единственный рычаг — HOME, и он должен совпадать
# с HOME рантайм-пользователя, иначе рантайм пойдёт качать модели заново.
ENV HOME=/home/app \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# onnxruntime (под капотом fastembed) линкуется с libgomp.
# В python:3.11-slim его нет — без этого пакета импорт fastembed падает.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install fastembed==0.8.0

# Имена моделей продублированы литералами намеренно: импорт reviews_agent
# здесь потребовал бы скопировать код ВЫШЕ этого слоя и убил бы весь смысл
# кэширования. Расхождение с кодом ловится проверкой в стадии 2.
RUN mkdir -p "$HOME/.cache/fastembed" \
 && python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; C='/home/app/.cache/fastembed'; TextEmbedding(model_name='intfloat/multilingual-e5-large', cache_dir=C); TextCrossEncoder(model_name='BAAI/bge-reranker-base', cache_dir=C); print('models warmed')"


# --------------------------------------------------------------------------
# Стадия 2: рантайм.
# --------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Реальный пользователь с реальным домашним каталогом.
# Path.home() читает $HOME, а при его отсутствии лезет в /etc/passwd —
# и падает с RuntimeError, если uid там не описан. Оба условия закрыты.
RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin app

COPY --from=models --chown=app:app /home/app/.cache/fastembed /home/app/.cache/fastembed

WORKDIR /app
COPY --chown=app:app . /app

RUN pip install /app

# Страховка от тихого расхождения: если в коде поменяют имя модели, а тут
# нет — сборка упадёт здесь, а не превратится в скачивание 3.2 ГБ в проде.
RUN python -c "from reviews_agent.embeddings import MODEL_NAME as E; from reviews_agent.rerank import MODEL_NAME as R; assert (E, R) == ('intfloat/multilingual-e5-large', 'BAAI/bge-reranker-base'), (E, R); print('model names match')"

USER app

EXPOSE 8080

# Настройки в CMD не читаются — uvicorn CLI имеет свои дефолты
# (127.0.0.1:8000), поэтому host и port заданы явно.
CMD ["uvicorn", "reviews_agent.api.service:app", "--host", "0.0.0.0", "--port", "8080"]