"""
FastAPI-сервис: приём отзывов, вебхук Telegram и служебные пробы.

ПОЧЕМУ ВЕБХУК, А НЕ POLLING
---------------------------
Polling проще завести, но он требует ЖИВОГО процесса, который сам ходит
в Telegram. На VPS под Docker это лишний долгоживущий исходящий коннект
и отдельная модель отказа. Вебхук укладывается в ту же схему, что и любой
другой HTTP-сервис за reverse-proxy: контейнер слушает порт, Telegram
стучится снаружи.

ЧТО ТРЕБУЕТСЯ СНАРУЖИ
---------------------
Telegram принимает ТОЛЬКО https и только с валидным сертификатом.
Значит, на VPS нужен reverse-proxy (nginx или Caddy) с Let's Encrypt,
проксирующий TELEGRAM_WEBHOOK_PATH на webhook_port контейнера.
Самоподписанный сертификат Telegram формально поддерживает, но это
лишняя нестандартная ветка — не стоит того.

ПОЧЕМУ ОЧЕРЕДЬ, А НЕ ОБРАБОТКА В ЗАПРОСЕ
----------------------------------------
Апдейт кладётся в application.update_queue, и эндпоинт немедленно
отвечает 200. Если обрабатывать внутри запроса, Telegram будет ждать
ответа всё время похода в Postgres — а не дождавшись, ПОВТОРИТ доставку.
Получили бы ретраи ровно на медленных операциях, то есть именно там,
где они опаснее всего. Идемпотентность у нас есть (повторный resume
упирается в ThreadNotPaused), но полагаться на неё вместо быстрого 200
неправильно.

ПРОВЕРКА СЕКРЕТА
----------------
Эндпоинт вебхука по определению открыт в интернет. Единственное, что
отличает Telegram от постороннего, — заголовок
X-Telegram-Bot-Api-Secret-Token, который Telegram присылает потому, что
мы задали secret_token в setWebhook. Сравнение через hmac.compare_digest,
а не '==': сравнение секретов обычным оператором утекает по времени.

ПРИЁМ ОТЗЫВОВ (POST /reviews)
-----------------------------
Раньше стартовый прогон запускался только руками:
`docker exec ... python -m scripts.run_graph <id>`. Эндпоинт переносит
приём внутрь сервиса, чтобы источник отзывов (n8n на том же VPS) мог
отдавать их сам.

Наружу НЕ открыт. Nginx проксирует только путь вебхука; n8n стоит
в той же docker-сети и ходит на http://reviews-agent:8080/reviews
напрямую. Изоляция сети и есть авторизация — отдельный общий секрет
добавил бы ещё одну переменную окружения и ничего бы не усилил.

202, А НЕ 200 С РЕЗУЛЬТАТОМ: пайплайн идёт секунды (Haiku, эмбеддинг,
реранк, Sonnet). Держать HTTP-соединение всё это время незачем —
результат всё равно уезжает не вызывающему, а оператору в Telegram.
Вызывающий получает подтверждение приёма, дальше работа идёт в фоне.

client_id НЕ ПРИНИМАЕТСЯ. Его ставит classify из настроек
(state.get("client_id") or settings.client_id). Раз classify предпочитает
значение из состояния, разрешить вызывающему прислать client_id значило
бы дать ему прочитать базу знаний ЧУЖОГО клиента. Модель отвергает любые
лишние поля (extra="forbid"), а не молча их игнорирует.

ЛОГИРОВАНИЕ
-----------
configure_logging() вызывается первой строкой lifespan — то есть при
любом способе запуска, включая голый uvicorn в контейнере. Порядок
критичен: вызов стоит ДО set_webhook, иначе самый первый запрос к
Telegram API успеет напечатать URL с токеном.

Запуск:
    python -m scripts.run_webhook
    # или напрямую:
    uvicorn reviews_agent.api.service:app --host 0.0.0.0 --port 8080
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import (
    BackgroundTasks,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from telegram import Update
from telegram.ext import Application

from reviews_agent.config import get_settings
from reviews_agent.hitl import start_review, thread_exists
from reviews_agent.logging_setup import configure_logging
from reviews_agent.tg.handlers import register_handlers
from reviews_agent.tg.sender import send_failure_notice_async, send_operator_card_async

logger = logging.getLogger(__name__)

settings = get_settings()

# Апдейты, которые нам нужны. Всё остальное Telegram даже не пришлёт —
# меньше мусора в очереди и меньше поверхности для сюрпризов.
ALLOWED_UPDATES = ["message", "callback_query"]

# Путь приёма отзывов. Литерал, а не настройка: наружу он не открыт,
# менять его незачем, а лишняя переменная окружения — лишний способ
# рассинхронизировать сервис и n8n.
REVIEWS_PATH = "/reviews"

# Метка «узла» в аварийных уведомлениях из приёма. Узлы графа
# называются classify / retrieve / generate, поэтому оператор сразу
# видит, что отказ инфраструктурный, а не в обработке отзыва.
INTAKE_NODE = "api/reviews"

# Отзывы, которые прямо сейчас крутятся в фоне.
#
# thread_exists() ходит в базу и потому не атомарен: два одновременных
# POST по одному review_id успели бы проскочить оба и дописать второй
# прогон в ту же нить. Множество под локом закрывает окно между
# проверкой и запуском.
#
# Процессная, а не распределённая защита — и этого достаточно: воркер
# uvicorn один, второго процесса, принимающего отзывы, не существует.
# Если появится — сюда придёт advisory lock в Postgres, а не второй
# набор костылей поверх этого.
#
# asyncio.Lock() создаётся до старта цикла событий: с Python 3.10
# примитивы asyncio привязываются к циклу лениво, при первом await.
_inflight: set[str] = set()
_inflight_lock = asyncio.Lock()


class ReviewIn(BaseModel):
    """
    Входной отзыв. Поля — ровно те, что читает make_initial_state.

    extra="forbid" здесь несёт смысл безопасности, а не аккуратности:
    именно он не даёт прислать client_id и увести обработку на чужую
    базу знаний. Молчаливое игнорирование лишних полей выглядело бы
    так же, но пускало бы отправителя в заблуждение.

    created_at остаётся строкой, а не datetime. Значение уходит
    в состояние графа и оттуда в чекпоинт как есть; разбор в datetime
    поменял бы то, что лежит в базе, ради валидации, которой никто
    не пользуется.
    """

    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    author: str = Field(min_length=1)
    rating: int = Field(ge=1, le=5)
    created_at: str = Field(min_length=1)
    text: str = Field(min_length=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Поднимает приложение Telegram и регистрирует вебхук.

    Первым делом — configure_logging(). При запуске через
    scripts/run_webhook.py логирование уже настроено и вызов ничего не
    меняет; при запуске голым uvicorn (Docker) это ЕДИНСТВЕННОЕ место,
    где оно вообще настраивается.

    .updater(None) обязателен: без него PTB собирает Updater для polling,
    который в вебхук-режиме не нужен и будет мешать.

    drop_pending_updates=True при старте — осознанно. Апдейты, накопившиеся
    пока сервис лежал, относятся к карточкам, чьё состояние в графе могло
    измениться. Разгребать их вслепую хуже, чем отбросить: оператор может
    запросить карточку заново через scripts/run_graph.py --card.
    """
    configure_logging()

    tg_app = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .updater(None)
        .build()
    )
    register_handlers(tg_app)

    await tg_app.initialize()
    await tg_app.start()

    webhook_url = settings.telegram_webhook_url
    if webhook_url:
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        logger.info("Вебхук зарегистрирован: %s", webhook_url)
    else:
        logger.warning(
            "TELEGRAM_WEBHOOK_BASE_URL пуст — setWebhook не вызывался. "
            "Сервис поднят, но Telegram в него не стучится."
        )

    app.state.tg_app = tg_app
    try:
        yield
    finally:
        await tg_app.stop()
        await tg_app.shutdown()
        logger.info("Приложение Telegram остановлено.")


app = FastAPI(title="reviews-agent HITL webhook", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Проба для proxy/оркестратора. Базу не трогает — это проверка процесса, не стека."""
    return {"status": "ok", "webhook": bool(settings.telegram_webhook_url)}


async def process_review(review: dict, tg_app: Application) -> None:
    """
    Фоновая обработка принятого отзыва: прогон графа и карточка оператору.

    start_review синхронный и внутри зовёт graph.invoke, поэтому уходит
    в asyncio.to_thread — ровно как handlers.py поступает с resume_thread.
    Прямой вызов заблокировал бы цикл событий на всё время работы моделей,
    и сервис перестал бы отвечать на нажатия кнопок по другим отзывам.

    Развилка повторяет do_start из scripts/run_graph.py — сознательно, это
    один и тот же контракт start_review, а не совпадение:
      * next пуст          — граф дошёл до конца без паузы (спам-ветка);
      * paused False       — граф встал не там, где должен;
      * иначе              — пауза на human_gate, шлём карточку.

    Про catch-all: отказы САМИХ УЗЛОВ сюда не долетают. error_handler на
    графе гасит исключение, сам уведомляет оператора и уводит граф в
    терминальный failed — то есть start_review возвращается штатно.
    Значит этот except ловит только то, о чём иначе не узнает никто:
    недоступный Postgres, сбой чекпоинтера, отказ отправки карточки.
    Двойного уведомления он не создаёт.
    """
    review_id = review["review_id"]
    try:
        result = await asyncio.to_thread(start_review, review)

        if not result["next"]:
            # Спам-ветка: spam_filter -> END. Карточки нет, потому что
            # решать нечего. Это штатная работа, а не отказ.
            logger.info(
                "Отзыв %s обработан без паузы (review_status=%r) — карточка не нужна.",
                review_id,
                result["state"].get("review_status"),
            )
            return

        if not result["paused"]:
            # Граф остановился не на human_gate. Отзыв застрял, и молча
            # оставить его в этом состоянии нельзя.
            logger.error(
                "Отзыв %s: граф встал на %s вместо human_gate.",
                review_id,
                result["next"],
            )
            await send_failure_notice_async(
                review_id=review_id,
                node=INTAKE_NODE,
                message=(
                    f"Граф остановился на {result['next']} вместо human_gate. "
                    f"Отзыв не доведён до карточки."
                ),
                review_text=review.get("text", ""),
                bot=tg_app.bot,
            )
            return

        message_id = await send_operator_card_async(result["payload"], bot=tg_app.bot)
        logger.info("Отзыв %s: карточка отправлена (message_id=%s).", review_id, message_id)

    except Exception as exc:
        logger.exception("Приём отзыва %s упал вне узлов графа.", review_id)
        # Уведомление тоже может не уйти — тогда в логах останутся обе
        # ошибки. Подменять исходную ошибку сбоем доставки нельзя.
        try:
            await send_failure_notice_async(
                review_id=review_id,
                node=INTAKE_NODE,
                message=str(exc),
                review_text=review.get("text", ""),
                bot=tg_app.bot,
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить оператора об отказе приёма отзыва %s.", review_id
            )
    finally:
        # Снимаем отметку в любом исходе. Если процесс умрёт раньше, чем
        # это выполнится, множество исчезнет вместе с ним — но нить в базе
        # останется, и повторный POST упрётся в 409. Это верное поведение:
        # недообработанный отзыв разбирает оператор через --reset, а не
        # тихий повторный прогон.
        _inflight.discard(review_id)


@app.post(REVIEWS_PATH, status_code=status.HTTP_202_ACCEPTED)
async def ingest_review(
    review: ReviewIn,
    background: BackgroundTasks,
    request: Request,
) -> dict:
    """
    Принимает отзыв, отвечает 202 и запускает обработку в фоне.

    409 на повторный review_id. Причина в том, что thread_id = review_id:
    повторный старт не начал бы прогон заново, а ДОПИСАЛ бы второй прогон
    к существующей нити. Автоматический reset был бы хуже отказа — он молча
    стёр бы паузу, на которую оператор в этот момент смотрит.

    Проверка и отметка «в работе» сделаны под одним локом, поэтому между
    ними не может вклиниться второй запрос. Лок держится на время похода
    в базу, то есть запросы приёма сериализуются — при отзывах единицами
    в час это не имеет цены, а гонку закрывает полностью.
    """
    review_id = review.review_id

    async with _inflight_lock:
        if review_id in _inflight:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Отзыв {review_id!r} уже обрабатывается.",
            )

        if await asyncio.to_thread(thread_exists, review_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Отзыв {review_id!r} уже обрабатывался ранее. "
                    f"Для повторного прогона нужен явный сброс нити."
                ),
            )

        _inflight.add(review_id)

    background.add_task(process_review, review.model_dump(), request.app.state.tg_app)

    logger.info("Отзыв %s принят, обработка запущена в фоне.", review_id)
    return {"accepted": True, "review_id": review_id}


@app.post(settings.telegram_webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Принимает апдейт, проверяет секрет, кладёт в очередь и сразу отвечает 200."""
    expected = settings.telegram_webhook_secret.get_secret_value()
    if expected:
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, expected
        ):
            logger.warning("Отклонён запрос вебхука с неверным secret_token.")
            raise HTTPException(status_code=403, detail="forbidden")

    tg_app: Application = request.app.state.tg_app
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)

    await tg_app.update_queue.put(update)
    return {"ok": True}