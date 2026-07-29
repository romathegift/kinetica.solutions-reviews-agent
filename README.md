# AI Reviews & Reputation Agent

Пайплайн на LangGraph: классифицирует отзывы клиентов, генерирует ответы
в тоне бренда с опорой на базу знаний (RAG), отправляет на подтверждение
человеку в Telegram.

**Статус:** в разработке. Портфолио-демо Kinetica Solutions.

## Что делает

1. Принимает отзыв (Google Maps), определяет язык — uk / en.
2. Классифицирует: категория, тональность, срочность, флаг эскалации.
3. Маршрутизирует по категории:
   - `spam` — отсекается без генерации;
   - `praise` — ответ сразу, без обращения к базе знаний;
   - `complaint` / `question` — сначала поиск в базе знаний (pgvector), потом ответ.
4. Все не-спам ответы уходят человеку на подтверждение в Telegram:
   approve / edit / reject.
5. После подтверждения — публикация (в демо — имитация).

## Стек

| Слой | Технология |
|---|---|
| Оркестрация | LangGraph 1.2.9 |
| LLM | Claude Haiku 4.5 (классификация), Claude Sonnet 5 (генерация) |
| Эмбеддинги | intfloat/multilingual-e5-large через fastembed (ONNX) |
| Векторный поиск | pgvector, HNSW, косинусная метрика |
| Персистентность | PostgresSaver (чекпоинты графа) |
| HITL | Telegram Bot API |
| API | FastAPI |
| Деплой | Docker + docker compose, Nginx + TLS |

## Структура

```
reviews_agent/
├── nodes/      — узлы графа (ingest, classify, retrieve, generate, ...)
├── prompts/    — промпты классификатора и генератора
├── tg/         — Telegram-клиент и HITL-карточки
└── api/        — FastAPI: webhook, health-check
data/           — фикстуры отзывов и база знаний
scripts/        — индексация базы знаний, прогон графа, запуск вебхука
tests/          — тесты
Dockerfile          — двухстадийная сборка, модели запекаются в образ
docker-compose.yml  — деплой на VPS
```

## Зависимости

Единственный источник правды — `pyproject.toml`. Отдельного
`requirements.txt` нет намеренно: два списка неизбежно расходятся,
а воспроизводимость даёт не ручной список, а машинный lock-файл
(`uv lock` / `pip-compile`), если он когда-нибудь понадобится.

## Локальная разработка

Python 3.11. Из корня репозитория:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # заполнить реальными значениями
```

База живёт в контейнере Postgres на VPS и наружу не проброшена,
поэтому локально нужен SSH-туннель:

```bash
ssh -f -N -L 127.0.0.1:5432:172.18.0.2:5432 \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=60 \
    root@178.105.80.161
```

Проверка туннеля перед работой:

```bash
python -c "from reviews_agent.db import check_connection; print(check_connection())"
```

Здоровое состояние: база `reviews`, пользователь `reviews_app`,
pgvector, 22 строки в `knowledge_base`.

Скрипты запускаются из корня репозитория:

```bash
python -m scripts.index_knowledge_base               # переиндексация после правок базы знаний
python -m scripts.run_graph <review_id>              # START: до HITL-паузы
python -m scripts.run_graph <review_id> approve      # аварийное возобновление без бота
python -m scripts.run_graph <review_id> --reset      # стереть нить перед чистым прогоном
python -m scripts.run_webhook                        # локальный запуск вебхук-сервиса
python -m pytest tests/ -v                           # тесты
```

`thread_id` равен `review_id`, поэтому повторный START по существующей
нити не возобновляет её, а дописывает новый прогон к старому.
Перед любым повторным прогоном фикстуры — всегда `--reset`.

## Деплой

Код на сервере — git-клон в `/opt/reviews-agent`, файлы попадают туда
только через `git pull`, не через scp. Конфиг приходит в контейнер
настоящими переменными окружения (`env_file` в compose), а не
смонтированным `.env`.

```bash
ssh root@178.105.80.161
cd /opt/reviews-agent
git pull
docker compose up -d --build    # --build обязателен: без него берётся старый образ
docker compose ps
docker logs --since 15m reviews-agent | grep -v /health
```

Образ собирается на самом сервере (x86_64), а не на ARM-маке.
Веса моделей (~3.2 ГБ) запечены в отдельный ранний слой, поэтому
правки кода их не пересобирают.

Операционные команды в проде:

```bash
docker exec reviews-agent python -m scripts.run_graph <review_id> --reset
docker exec reviews-agent python -m scripts.run_graph <review_id>
```

Два правила: локальный сервис и контейнер не должны работать
одновременно — оба вызывают `setWebhook` на один URL и перехватывают
апдейты друг у друга; порт 8080 на loopback сервера должен быть
свободен от обратного SSH-туннеля.

Nginx терминирует TLS на `reviews.kinetica.solutions` и проксирует
только `/telegram/webhook` на `127.0.0.1:8080`. `/health` наружу
не открыт.

## Лицензия

Демонстрационный проект. Бренд «Медовий Ранок» вымышлен,
отзывы — синтетические фикстуры.