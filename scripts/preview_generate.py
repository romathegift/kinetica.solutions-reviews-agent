"""
Превью генерации: прогон фикстур через цепочку узлов БЕЗ графа.

Запуск из корня проекта при поднятом SSH-туннеле:
    python -m scripts.preview_generate            # все фикстуры
    python -m scripts.preview_generate rev_004    # одна фикстура

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ ДОЖДАТЬСЯ ГРАФА (шаг 7):
граф добавит чекпоинты, interrupt() и HITL — то есть новые причины падать.
Проверять промпт генерации сквозь них — значит смешивать два источника
ошибок: «модель написала не то» и «граф собран не так». Здесь узлы вызываются
напрямую, а состояние мержится руками — ровно то, что делает LangGraph,
но без единой его строки.

ЖИВЁТ ДАЛЬШЕ ШАГА 6: это инструмент калибровки промпта. Правка
SYSTEM_PROMPT_TEMPLATE проверяется одной командой, без графа и без Telegram.
По той же причине рядом стоит calibrate_threshold.py — он зовёт search_facts
напрямую.

ТОПОЛОГИЯ ГРАФА ВОСПРОИЗВЕДЕНА ЗДЕСЬ КОНСТАНТАМИ, а не импортом роутера:
роутер отдаёт имена узлов LangGraph, и звать его вне графа — значит
переводить его ответ обратно в вызовы функций. Дублирование маршрута
осознанное и ограничено двумя строками; настоящую топологию проверит граф
на шаге 7.

СТОИТ ДЕНЕГ: 6 вызовов Sonnet 5 на полный прогон (spam до генерации не
доходит) плюс 7 вызовов Haiku. Одна фикстура аргументом — один вызов.

НЕДЕТЕРМИНИРОВАН ПО ЧЕРНОВИКУ: два прогона дадут разный текст. Это не баг
скрипта, а требование к генерации — под соседними похожими отзывами бренд
не должен публиковать ответы-близнецы. Никаким параметром это не задаётся:
Sonnet 5 отвергает temperature с любым недефолтным значением, и модель
сэмплирует на своём дефолте. Стабильны здесь классификация, поиск и флаги —
их и сверяем.
"""

import json
import sys

from reviews_agent.config import PROJECT_ROOT
from reviews_agent.db import close_pool
from reviews_agent.nodes.classify import classify
from reviews_agent.nodes.generate import generate
from reviews_agent.nodes.ingest import ingest
from reviews_agent.nodes.retrieve import retrieve
from reviews_agent.state import ReviewState

REVIEWS_FILE = PROJECT_ROOT / "data" / "reviews.json"
KB_FILE = PROJECT_ROOT / "data" / "knowledge_base.json"

# Ветки, которые ходят в Retrieve. praise минует его: подяке факты не нужны.
NEEDS_RETRIEVE = ("complaint", "question")

# Ветки, которые доходят до Generate. spam уводится роутером в spam_filter
# (review_status=skipped) и до Sonnet не доходит — ни ответа, ни расходов.
NEEDS_GENERATE = ("complaint", "question", "praise")

# Поля, которые фикстура обещает в _expected. Порядок фиксирован —
# по нему печатается строка сверки.
CHECKED_FIELDS = ("language", "category", "sentiment", "urgency", "escalate")


def load_data() -> tuple[list[dict], dict[str, str]]:
    """
    Читает фикстуры и карту «текст чанка -> его id».

    Карта по content, а не по id: в базе идентификаторы автоинкрементные
    и меняются при каждой переиндексации, а стабильные kb_001... живут
    только в JSON. Тот же приём, что в calibrate_threshold.py.
    """
    with REVIEWS_FILE.open(encoding="utf-8") as f:
        reviews = json.load(f)

    with KB_FILE.open(encoding="utf-8") as f:
        chunks = json.load(f)

    return reviews, {chunk["content"]: chunk["id"] for chunk in chunks}


def run_pipeline(fixture: dict) -> ReviewState:
    """
    Прогоняет один отзыв по цепочке узлов, мержа состояние вручную.

    _expected в состояние НЕ попадает: это ожидания теста, а не данные отзыва.
    client_id тоже не кладём — его ставит classify из настроек, и пусть
    ставит: подсунув его здесь, мы бы скрыли поломку этого пути.

    Маршрут считается по ФАКТИЧЕСКОЙ категории из classify, а не по ожидаемой
    из фикстуры. Иначе ошибка классификатора поехала бы по правильной ветке
    и осталась незамеченной.
    """
    state: ReviewState = {k: v for k, v in fixture.items() if k != "_expected"}

    state.update(ingest(state))
    state.update(classify(state))

    if state["category"] in NEEDS_RETRIEVE:
        state.update(retrieve(state))

    if state["category"] in NEEDS_GENERATE:
        state.update(generate(state))

    return state


def check_fields(fixture: dict, state: ReviewState) -> bool:
    """Печатает сверку полей классификации. Возвращает True, если всё сошлось."""
    expected = fixture["_expected"]

    parts = []
    ok = True
    for field in CHECKED_FIELDS:
        actual = state.get(field)
        match = actual == expected[field]
        ok = ok and match
        parts.append(f"{field}={actual}{'✓' if match else '✗'}")

    print("\n  поля:    " + "  ".join(parts))
    if not ok:
        print(
            "           очікувалось: "
            + "  ".join(f"{f}={expected[f]}" for f in CHECKED_FIELDS)
        )

    if state.get("escalate"):
        print(f"  причина: {state.get('escalation_reason')}")

    return ok


def check_context(
    fixture: dict, state: ReviewState, content_to_id: dict[str, str]
) -> bool:
    """
    Печатает сверку найденного контекста. Возвращает True, если всё сошлось.

    СЕМАНТИКА kb_ids — ПОЛ, А НЕ РАВЕНСТВО. Список означает «это обязано быть
    найдено», а не «найдено ровно это»: лишний faq ошибкой не является, потому
    что порог question намеренно мягкий (потерянный факт дорог, лишний
    бесплатен). Так же считает scripts/calibrate_threshold.py — две метрики
    на один и тот же поиск обязаны показывать одно и то же.

    ПУСТОЙ СПИСОК — ИСКЛЮЧЕНИЕ, и означает он обратное: не «не проверяем»,
    а «не должно пройти НИЧЕГО». Это rev_004 — единственная фикстура, где
    найденный чанк является ошибкой: платёжных инцидентов в базе нет намеренно,
    и любой подтянувшийся policy означает, что агент пообещает не то.
    Поэтому и вердикт у неё свой: «очікуване знайдено» под пустым списком
    было бы бессмыслицей — там ничего и не ожидалось.
    """
    category = state["category"]

    if category not in NEEDS_RETRIEVE:
        # Печатаем ФАКТИЧЕСКУЮ ветку, а не «praise» константой: спам тоже
        # минует Retrieve, и подпись «гілка praise» под спамом была бы враньём
        # в выводе инструмента, которым калибруют промпт.
        print(f"  факти:   Retrieve минуємо (гілка {category})")
        return True

    found = [
        content_to_id.get(c["content"], "?") for c in state.get("context_chunks", [])
    ]
    wanted = fixture["_expected"]["kb_ids"]

    print(f"  факти:   {found if found else 'порожньо'}")

    # Ветка «не должно пройти ничего»: здесь ошибка — это ЛЮБАЯ находка.
    if not wanted:
        if found:
            print(f"           ✗ протікання, мало бути порожньо: {found}")
            return False
        print("           ✓ порожньо — рішення в базі немає, це й перевіряємо")
        return True

    misses = [chunk_id for chunk_id in wanted if chunk_id not in found]
    if misses:
        print(f"           ✗ не знайдено очікуване: {misses}")
        return False

    extra = [chunk_id for chunk_id in found if chunk_id not in wanted]
    print("           ✓ очікуване знайдено" + (f", понад нього: {extra}" if extra else ""))
    return True


def report(fixture: dict, state: ReviewState, content_to_id: dict[str, str]) -> bool:
    """
    Печатает результат по одной фикстуре. Возвращает True, если сверка прошла.

    Сверяются только ДЕТЕРМИНИРОВАННЫЕ поля: классификация, эскалация и состав
    найденного контекста. Черновик и флаги печатаются для чтения глазами —
    ожидаемого текста у генерации быть не может.
    """
    expected = fixture["_expected"]

    print("\n" + "=" * 78)
    print(f"{fixture['review_id']}  ({expected['category']}, {expected['language']})")
    print("=" * 78)
    print(f"  відгук: {fixture['text'][:70]}...")

    fields_ok = check_fields(fixture, state)
    context_ok = check_context(fixture, state, content_to_id)

    # --- Статика ---
    brand = state.get("brand_context", [])
    if brand:
        voice = sum(1 for c in brand if c["type"] == "voice")
        example = sum(1 for c in brand if c["type"] == "example")
        langs = sorted({c["language"] for c in brand})
        print(f"  статика: voice {voice}, example {example}, мови {langs}")

    # --- Черновик ---
    if state["category"] in NEEDS_GENERATE:
        print(f"\n  ЧЕРНЕТКА:\n    {state.get('draft', '')}")
        flags = state.get("guardrail_flags", [])
        print(f"\n  прапорці: {', '.join(flags) if flags else 'немає'}")
    else:
        print("\n  ЧЕРНЕТКА: не генерується — роутер веде спам у spam_filter")

    return fields_ok and context_ok


def main() -> None:
    reviews, content_to_id = load_data()

    # Аргумент фильтрует фикстуры: калибровать промпт на одной фикстуре
    # дешевле и быстрее, чем гонять все шесть.
    if len(sys.argv) > 1:
        wanted = set(sys.argv[1:])
        reviews = [r for r in reviews if r["review_id"] in wanted]
        if not reviews:
            print(f"Фикстуры не найдены: {', '.join(sorted(wanted))}")
            return

    results = {r["review_id"]: report(r, run_pipeline(r), content_to_id) for r in reviews}

    passed = sum(results.values())
    print("\n" + "=" * 78)
    print(f"СВЕРКА ДЕТЕРМИНИРОВАННЫХ ПОЛЕЙ: {passed}/{len(results)}")
    failed = [rid for rid, is_ok in results.items() if not is_ok]
    if failed:
        print(f"Расхождения: {', '.join(failed)}")
    print(
        "\nЧерновики читать глазами: модель сэмплирует, ожидаемого текста нет.\n"
        "rev_004 — ответ обязан признать проблему БЕЗ обещаний.\n"
        "rev_006 — факты в базе только на украинском; в английском ответе\n"
        "          не должно остаться ни одной кириллической буквы.\n"
        "rev_005 — обе темы (молоко и собака), а не одна."
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()