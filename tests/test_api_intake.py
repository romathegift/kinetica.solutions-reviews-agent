"""
Тесты приёма отзывов (POST /reviews).

Принципы, по которым написаны тесты:

  * БЕЗ сети, моделей, БД и Telegram. Всё, что ходит наружу, подменяется:
    thread_exists и start_review (Postgres), send_operator_card_async и
    send_failure_notice_async (Telegram API).

  * LIFESPAN НЕ ЗАПУСКАЕТСЯ. TestClient используется БЕЗ контекстного
    менеджера — именно поэтому lifespan не выполняется и setWebhook не
    вызывается. Это не деталь оформления, а требование: живой setWebhook
    из тестов увёл бы апдейты Telegram с рабочего контейнера на VPS.
    app.state.tg_app подставляется заглушкой руками.

  * Фоновые задачи ВЫПОЛНЯЮТСЯ ДО ВОЗВРАТА ОТВЕТА. TestClient прогоняет
    весь ASGI-цикл, включая BackgroundTasks, поэтому после client.post()
    результат фоновой обработки уже можно проверять. В проде так не
    происходит — там вызывающий получает 202 раньше, — но контракт
    самой обработки от этого не меняется.

  * Форма входа НЕ ХАРДКОДИТСЯ дважды. test_fixtures_match_contract
    прогоняет через ReviewIn реальные отзывы из data/reviews.json:
    если контракт эндпоинта и фикстуры разъедутся, падать будет тест,
    а не прод.

  * Главный тест безопасности — test_rejects_client_id. client_id не
    входит в модель, и extra="forbid" обязан его отвергнуть: возможность
    прислать чужой client_id означала бы чтение чужой базы знаний.

Импорт api.service читает настройки на уровне модуля, поэтому тестам
нужен заполненный .env (локально) или переменные окружения (контейнер).
Ключи при этом никуда не уходят: ни один вызов наружу не выполняется.

Запуск из корня репозитория:  python -m pytest tests/ -v
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from reviews_agent.api import service
from reviews_agent.api.service import INTAKE_NODE, REVIEWS_PATH, ReviewIn

FIXTURES_PATH = Path(__file__).resolve().parent.parent / "data" / "reviews.json"

# Поля, которые эндпоинт обязан передать в граф — ровно то, что читает
# make_initial_state. Список зашит намеренно: тест должен падать, если
# контракт расширят, не подумав про client_id.
EXPECTED_KEYS = {"review_id", "source", "author", "rating", "created_at", "text"}

VALID_REVIEW = {
    "review_id": "rev_test_001",
    "source": "google_maps",
    "author": "Тестова Особа",
    "rating": 4,
    "created_at": "2026-07-29T10:00:00Z",
    "text": "Кава добра, але чекала довго.",
}

# Нагрузка interrupt(), какой её отдаёт start_review на паузе.
# Внутрь не заглядываем: эндпоинт передаёт её отправке как есть.
PAUSE_PAYLOAD = {"review_id": VALID_REVIEW["review_id"], "draft": "чернетка"}


class Recorder:
    """Собирает вызовы подменённых функций, чтобы тесты их проверяли."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.cards: list[tuple[dict, object]] = []
        self.failures: list[dict] = []
        self.exists_calls: list[str] = []


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def bot() -> object:
    """Заглушка бота. Сравнивается по идентичности — метод на ней не зовут."""
    return object()


@pytest.fixture
def client(monkeypatch, rec, bot) -> TestClient:
    """
    Клиент с подменёнными внешними зависимостями.

    По умолчанию: нити нет, граф встаёт на паузу, отправка успешна.
    Отдельные тесты переопределяют нужное через monkeypatch.
    """
    # Состояние приёма — модульное, поэтому чистится на каждый тест.
    service._inflight.clear()
    # Свежий лок на каждый тест: TestClient поднимает свой цикл событий,
    # и переиспользованный лок мог бы остаться привязанным к чужому.
    service._inflight_lock = asyncio.Lock()

    service.app.state.tg_app = SimpleNamespace(bot=bot)

    def fake_thread_exists(review_id: str) -> bool:
        rec.exists_calls.append(review_id)
        return False

    def fake_start_review(review: dict) -> dict:
        rec.started.append(review)
        return {
            "review_id": review["review_id"],
            "state": {"review_status": "pending"},
            "paused": True,
            "next": ("human_gate",),
            "payload": PAUSE_PAYLOAD,
        }

    async def fake_send_card(payload: dict, bot=None) -> int:
        rec.cards.append((payload, bot))
        return 999

    async def fake_send_failure(
        review_id: str, node: str, message: str, review_text: str = "", bot=None
    ) -> int:
        rec.failures.append(
            {
                "review_id": review_id,
                "node": node,
                "message": message,
                "review_text": review_text,
                "bot": bot,
            }
        )
        return 1000

    monkeypatch.setattr(service, "thread_exists", fake_thread_exists)
    monkeypatch.setattr(service, "start_review", fake_start_review)
    monkeypatch.setattr(service, "send_operator_card_async", fake_send_card)
    monkeypatch.setattr(service, "send_failure_notice_async", fake_send_failure)

    return TestClient(service.app)


# ======================================================================
# Контракт входа
# ======================================================================
def test_accepts_valid_review(client, rec):
    """Нормальный отзыв: 202, прогон запущен, карточка ушла оператору."""
    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "review_id": "rev_test_001"}
    assert len(rec.started) == 1
    assert len(rec.cards) == 1
    assert rec.cards[0][0] == PAUSE_PAYLOAD
    assert not rec.failures


def test_card_reuses_live_bot(client, rec, bot):
    """
    В отправку передаётся ИМЕННО бот работающего приложения.

    Без этого открывался бы второй HTTP-клиент к Telegram на каждый отзыв —
    ровно то, ради чего sender принимает bot параметром.
    """
    client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert rec.cards[0][1] is bot


def test_forwards_exactly_the_graph_contract(client, rec):
    """В граф уходят ровно шесть полей — ни больше, ни меньше."""
    client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert set(rec.started[0]) == EXPECTED_KEYS


def test_rejects_client_id(client, rec):
    """
    client_id от вызывающего НЕ принимается.

    Главный тест безопасности файла. classify берёт client_id из состояния,
    если оно там есть, поэтому принятое извне значение увело бы обработку
    на базу знаний другого клиента.
    """
    payload = {**VALID_REVIEW, "client_id": "somebody_else"}

    response = client.post(REVIEWS_PATH, json=payload)

    assert response.status_code == 422
    assert not rec.started, "Граф не должен запускаться на отвергнутом входе."


def test_rejects_unknown_field(client, rec):
    """Любое лишнее поле — отказ, а не молчаливое игнорирование."""
    response = client.post(REVIEWS_PATH, json={**VALID_REVIEW, "sentiment": "positive"})

    assert response.status_code == 422
    assert not rec.started


@pytest.mark.parametrize("field", sorted(EXPECTED_KEYS))
def test_rejects_missing_field(client, rec, field):
    """Отсутствие любого обязательного поля — отказ."""
    payload = {k: v for k, v in VALID_REVIEW.items() if k != field}

    response = client.post(REVIEWS_PATH, json=payload)

    assert response.status_code == 422
    assert not rec.started


@pytest.mark.parametrize("rating", [0, 6, -1, 100])
def test_rejects_rating_out_of_range(client, rec, rating):
    """Оценка вне 1..5 — отказ: она попадает в промпт генератора."""
    response = client.post(REVIEWS_PATH, json={**VALID_REVIEW, "rating": rating})

    assert response.status_code == 422
    assert not rec.started


@pytest.mark.parametrize("field", ["review_id", "text", "author", "source", "created_at"])
def test_rejects_empty_strings(client, rec, field):
    """
    Пустая строка — отказ.

    Пустой review_id дал бы нить с пустым thread_id, пустой text — прогон
    моделей по пустому отзыву. И то и другое стоит денег и ничего не даёт.
    """
    response = client.post(REVIEWS_PATH, json={**VALID_REVIEW, field: ""})

    assert response.status_code == 422
    assert not rec.started


def test_fixtures_match_contract():
    """
    Реальные фикстуры проходят через модель эндпоинта.

    Защита от расхождения: если в data/reviews.json появится поле, которого
    нет в ReviewIn, или исчезнет обязательное — упадёт здесь, а не в проде.
    _expected снимается: это ожидания теста, а не данные отзыва.
    """
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    assert fixtures, "Фикстуры пусты — проверять нечего."

    for fixture in fixtures:
        review = {k: v for k, v in fixture.items() if k != "_expected"}
        ReviewIn(**review)  # ValidationError провалит тест сам


def test_model_rejects_client_id_directly():
    """Тот же запрет на уровне модели, без HTTP — чтобы причина была видна."""
    with pytest.raises(ValidationError):
        ReviewIn(**VALID_REVIEW, client_id="somebody_else")


# ======================================================================
# Защита от повторной обработки
# ======================================================================
def test_conflict_when_thread_exists(client, monkeypatch, rec):
    """Нить уже есть — 409, и граф не трогаем."""
    monkeypatch.setattr(service, "thread_exists", lambda review_id: True)

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 409
    assert not rec.started, "Повторный старт дописал бы второй прогон в ту же нить."
    assert not rec.cards


def test_conflict_when_already_inflight(client, rec):
    """
    Отзыв уже крутится в фоне — 409 ещё до похода в базу.

    Проверка идёт по множеству «в работе», поэтому thread_exists не
    вызывается вовсе: незачем ходить в Postgres за ответом, который
    уже известен процессу.
    """
    service._inflight.add(VALID_REVIEW["review_id"])

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 409
    assert not rec.exists_calls
    assert not rec.started


def test_inflight_released_after_success(client):
    """После успешной обработки отметка снята — иначе отзыв залипнет в 409."""
    client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert VALID_REVIEW["review_id"] not in service._inflight


def test_inflight_released_after_failure(client, monkeypatch):
    """И после отказа тоже: finally обязан отработать на любом исходе."""

    def boom(review: dict) -> dict:
        raise RuntimeError("Postgres недоступен")

    monkeypatch.setattr(service, "start_review", boom)

    client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert VALID_REVIEW["review_id"] not in service._inflight


# ======================================================================
# Исходы фоновой обработки
# ======================================================================
def test_spam_branch_sends_no_card(client, monkeypatch, rec):
    """
    Спам доходит до конца без паузы — карточки нет, и это не отказ.

    Пустой next означает, что граф завершился: spam_filter -> END.
    Уведомлять оператора не о чем, решать нечего.
    """

    def spam_result(review: dict) -> dict:
        return {
            "review_id": review["review_id"],
            "state": {"review_status": "spam"},
            "paused": False,
            "next": (),
            "payload": None,
        }

    monkeypatch.setattr(service, "start_review", spam_result)

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 202
    assert not rec.cards
    assert not rec.failures


def test_unexpected_stop_notifies_operator(client, monkeypatch, rec):
    """
    Граф встал не на human_gate — оператор обязан узнать.

    Отзыв застрял: карточки не будет, и промолчать здесь значило бы
    потерять его беззвучно.
    """

    def stuck_result(review: dict) -> dict:
        return {
            "review_id": review["review_id"],
            "state": {"review_status": "pending"},
            "paused": False,
            "next": ("retrieve",),
            "payload": None,
        }

    monkeypatch.setattr(service, "start_review", stuck_result)

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 202
    assert not rec.cards
    assert len(rec.failures) == 1
    assert rec.failures[0]["node"] == INTAKE_NODE
    assert "retrieve" in rec.failures[0]["message"]


def test_infrastructure_failure_notifies_operator(client, monkeypatch, rec, bot):
    """
    Отказ ВНЕ узлов графа уводится оператору с меткой приёма.

    Падения самих узлов сюда не долетают — их гасит error_handler на графе
    и сам уведомляет. Этот путь ловит остальное: недоступный Postgres,
    сбой чекпоинтера. Метка node отличает такой отказ от отказа узла.
    """

    def boom(review: dict) -> dict:
        raise RuntimeError("Postgres недоступен")

    monkeypatch.setattr(service, "start_review", boom)

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 202, "Приём уже подтверждён — отказ приходит позже."
    assert not rec.cards
    assert len(rec.failures) == 1
    assert rec.failures[0]["node"] == INTAKE_NODE
    assert "Postgres недоступен" in rec.failures[0]["message"]
    assert rec.failures[0]["review_text"] == VALID_REVIEW["text"]
    assert rec.failures[0]["bot"] is bot


def test_card_send_failure_notifies_operator(client, monkeypatch, rec):
    """
    Сбой отправки самой карточки тоже не теряется.

    Прогон уже оплачен и пауза сохранена, поэтому важно, чтобы оператор
    узнал и мог запросить карточку заново, а не считал отзыв необработанным.
    """

    async def card_boom(payload: dict, bot=None) -> int:
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(service, "send_operator_card_async", card_boom)

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 202
    assert len(rec.failures) == 1
    assert rec.failures[0]["node"] == INTAKE_NODE
    assert "Telegram недоступен" in rec.failures[0]["message"]


def test_notice_failure_does_not_crash_request(client, monkeypatch, rec):
    """
    Если и уведомление не уходит — запрос всё равно завершается.

    Обе ошибки остаются в логах; подменять исходную ошибку сбоем доставки
    нельзя, но и падать наружу фоновой задаче незачем.
    """

    def boom(review: dict) -> dict:
        raise RuntimeError("Postgres недоступен")

    async def notice_boom(*args, **kwargs) -> int:
        raise RuntimeError("Telegram тоже недоступен")

    monkeypatch.setattr(service, "start_review", boom)
    monkeypatch.setattr(service, "send_failure_notice_async", notice_boom)

    response = client.post(REVIEWS_PATH, json=VALID_REVIEW)

    assert response.status_code == 202
    assert VALID_REVIEW["review_id"] not in service._inflight