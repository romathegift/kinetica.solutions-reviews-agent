"""
Тесты кэша статического контекста (nodes/generate.py).

Принципы, по которым написаны тесты:

  * БЕЗ БАЗЫ. get_connection подменяется фейком, который повторяет условие
    реального SELECT (client_id + voice ИЛИ example нужной категории) и
    ПИШЕТ КАЖДЫЙ ЗАПРОС В ЖУРНАЛ. Кэш проверяется по длине этого журнала:
    «попал в кэш» — значит запроса не было вовсе, а не «ответ совпал».

  * ГЛАВНОЕ СВОЙСТВО — test_language_change_does_not_reread. Язык не входит
    в ключ кэша сознательно: SQL тянет обе локали разом. Если кто-то добавит
    язык в ключ, тесты на совпадение результата этого НЕ заметят, а этот
    заметит.

  * ВТОРОЕ ГЛАВНОЕ — test_result_objects_are_fresh. Кэш живёт всё время
    процесса, а brand_context уходит в состояние графа и в чекпоинт. Общий
    изменяемый объект оттуда испортил бы состояние отзыва, который к правке
    отношения не имеет, и найти это было бы почти невозможно.

  * Кэш сбрасывается ПЕРЕД КАЖДЫМ тестом autouse-фикстурой. Без этого
    порядок тестов начал бы влиять на результат — а такие падения читаются
    как «мигает», хотя мигает состояние.

Запуск из корня репозитория:  python -m pytest tests/ -v
"""

import pytest

from reviews_agent.nodes import generate as generate_module
from reviews_agent.nodes.generate import (
    clear_static_context_cache,
    fetch_static_context,
)

# Синтетическая база знаний. Устроена так, чтобы покрыть развилки:
#   c1: voice на обоих языках, complaint на обоих, praise ТОЛЬКО uk;
#   c2: voice есть, примеров нет вовсе.
# Поле category здесь нужно фейку для фильтрации — реальный SELECT его
# не возвращает, только использует в WHERE.
KB = [
    {"client_id": "c1", "content": "Тон: тепло", "type": "voice",
     "language": "uk", "source": "kb_001", "category": None},
    {"client_id": "c1", "content": "Tone: warm", "type": "voice",
     "language": "en", "source": "kb_002", "category": None},
    {"client_id": "c1", "content": "Приклад скарги", "type": "example",
     "language": "uk", "source": "kb_003", "category": "complaint"},
    {"client_id": "c1", "content": "Complaint example", "type": "example",
     "language": "en", "source": "kb_004", "category": "complaint"},
    {"client_id": "c1", "content": "Приклад подяки", "type": "example",
     "language": "uk", "source": "kb_005", "category": "praise"},
    {"client_id": "c2", "content": "Інший клієнт", "type": "voice",
     "language": "uk", "source": "kb_100", "category": None},
]

SELECTED_FIELDS = ("content", "type", "language", "source")


class _FakeCursor:
    """Курсор, повторяющий условие реального запроса и пишущий вызовы в журнал."""

    def __init__(self, log: list) -> None:
        self._log = log
        self._result: list[dict] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params: tuple) -> "_FakeCursor":
        client_id, category = params
        self._log.append((client_id, category))
        self._result = [
            {field: row[field] for field in SELECTED_FIELDS}
            for row in KB
            if row["client_id"] == client_id
            and (
                row["type"] == "voice"
                or (row["type"] == "example" and row["category"] == category)
            )
        ]
        return self

    def fetchall(self) -> list[dict]:
        return list(self._result)


class _FakeConnection:
    def __init__(self, log: list) -> None:
        self._log = log

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def cursor(self, row_factory=None) -> _FakeCursor:
        return _FakeCursor(self._log)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кэш модульный — без сброса порядок тестов влиял бы на результат."""
    clear_static_context_cache()
    yield
    clear_static_context_cache()


@pytest.fixture
def queries(monkeypatch) -> list:
    """Журнал запросов в базу. Пустой — значит запроса не было."""
    log: list = []
    monkeypatch.setattr(
        generate_module, "get_connection", lambda: _FakeConnection(log)
    )
    return log


# ======================================================================
# Попадания и промахи
# ======================================================================
def test_first_call_reads_db(queries):
    """Холодный кэш — ровно одно чтение."""
    fetch_static_context(client_id="c1", language="uk", category="complaint")

    assert queries == [("c1", "complaint")]


def test_repeat_call_does_not_reread(queries):
    """Тот же ключ — запроса нет вовсе."""
    for _ in range(5):
        fetch_static_context(client_id="c1", language="uk", category="complaint")

    assert len(queries) == 1


def test_language_change_does_not_reread(queries):
    """
    Смена языка НЕ трогает базу.

    Ключевое свойство: язык в SQL не участвует, обе локали приходят одним
    запросом. Добавь кто-нибудь язык в ключ кэша — тесты на содержимое
    результата останутся зелёными, а этот покраснеет.
    """
    fetch_static_context(client_id="c1", language="uk", category="complaint")
    fetch_static_context(client_id="c1", language="en", category="complaint")

    assert len(queries) == 1


def test_category_change_rereads(queries):
    """Категория входит в SQL, значит и в ключ: новое чтение обязательно."""
    fetch_static_context(client_id="c1", language="uk", category="complaint")
    fetch_static_context(client_id="c1", language="uk", category="praise")

    assert queries == [("c1", "complaint"), ("c1", "praise")]


def test_client_change_rereads(queries):
    """
    Разные клиенты не делят кэш.

    Если бы делили, один деплой отвечал бы голосом другого бренда — тихо
    и без единой ошибки в логах.
    """
    fetch_static_context(client_id="c1", language="uk", category="complaint")
    fetch_static_context(client_id="c2", language="uk", category="complaint")

    assert queries == [("c1", "complaint"), ("c2", "complaint")]


def test_clear_forces_reread(queries):
    """Явная инвалидация действительно инвалидирует."""
    fetch_static_context(client_id="c1", language="uk", category="complaint")
    clear_static_context_cache()
    fetch_static_context(client_id="c1", language="uk", category="complaint")

    assert len(queries) == 2


# ======================================================================
# Безопасность выдачи
# ======================================================================
def test_result_objects_are_fresh(queries):
    """
    Каждый вызов отдаёт СВОИ объекты, а не общие из кэша.

    brand_context уходит в состояние графа и в чекпоинт. Общий изменяемый
    словарь означал бы, что правка в одном отзыве меняет контекст другого —
    дефект, который в логах не виден вовсе.
    """
    voice_first, examples_first, _ = fetch_static_context(
        client_id="c1", language="uk", category="complaint"
    )
    voice_first[0]["content"] = "ИСПОРЧЕНО"
    examples_first.append("мусор")

    voice_second, examples_second, _ = fetch_static_context(
        client_id="c1", language="uk", category="complaint"
    )

    assert voice_second[0]["content"] == "Тон: тепло"
    assert len(examples_second) == 1
    assert voice_second[0] is not voice_first[0]


def test_cached_rows_are_immutable(queries):
    """Кэш хранит кортежи: испортить его содержимое снаружи нельзя в принципе."""
    fetch_static_context(client_id="c1", language="uk", category="complaint")

    cached = generate_module._fetch_static_rows("c1", "complaint")

    assert isinstance(cached, tuple)
    assert all(isinstance(row, tuple) for row in cached)


# ======================================================================
# Логика выбора не пострадала от кэша
# ======================================================================
def test_picks_requested_language(queries):
    """Украинский отзыв получает украинскую статику."""
    voice, examples, incomplete = fetch_static_context(
        client_id="c1", language="uk", category="complaint"
    )

    assert [chunk["source"] for chunk in voice] == ["kb_001"]
    assert [chunk["source"] for chunk in examples] == ["kb_003"]
    assert incomplete is False


def test_picks_english_from_same_cached_rows(queries):
    """Английский отзыв получает английскую статику — из тех же кэшированных строк."""
    fetch_static_context(client_id="c1", language="uk", category="complaint")
    voice, examples, incomplete = fetch_static_context(
        client_id="c1", language="en", category="complaint"
    )

    assert [chunk["source"] for chunk in voice] == ["kb_002"]
    assert [chunk["source"] for chunk in examples] == ["kb_004"]
    assert incomplete is False
    assert len(queries) == 1


def test_language_fallback_marks_incomplete(queries):
    """
    Примера на нужном языке нет — берём чужой язык и поднимаем флаг.

    Фолбэк идёт по ЯЗЫКУ, никогда по категории: бодрый пример подяки под
    жалобой кодом не ловится, а промах по языку ловит LANG_MISMATCH.
    """
    voice, examples, incomplete = fetch_static_context(
        client_id="c1", language="en", category="praise"
    )

    assert [chunk["source"] for chunk in voice] == ["kb_002"]
    assert [chunk["source"] for chunk in examples] == ["kb_005"]
    assert incomplete is True


def test_missing_examples_entirely(queries):
    """Примеров нет ни на одном языке — пустой список и флаг неполноты."""
    voice, examples, incomplete = fetch_static_context(
        client_id="c2", language="uk", category="complaint"
    )

    assert [chunk["source"] for chunk in voice] == ["kb_100"]
    assert examples == []
    assert incomplete is True


def test_voice_ignores_category(queries):
    """
    voice не фильтруется по категории.

    Правила тона категориям не принадлежат: они обязаны действовать
    и на жалобе, и на подяке.
    """
    voice_complaint, _, _ = fetch_static_context(
        client_id="c1", language="uk", category="complaint"
    )
    voice_praise, _, _ = fetch_static_context(
        client_id="c1", language="uk", category="praise"
    )

    assert [chunk["source"] for chunk in voice_complaint] == ["kb_001"]
    assert [chunk["source"] for chunk in voice_praise] == ["kb_001"]


def test_static_chunks_have_no_scores(queries):
    """Статика не ранжируется — скоров у неё нет и быть не должно."""
    voice, examples, _ = fetch_static_context(
        client_id="c1", language="uk", category="complaint"
    )

    for chunk in voice + examples:
        assert chunk["vector_score"] is None
        assert chunk["rerank_score"] is None