"""
Калибровка порогов реранкера для Retrieve.

Запуск из корня проекта при поднятом SSH-туннеле:
    python -m scripts.calibrate_threshold

Прогоняет фикстуры веток complaint и question через двухступенчатый поиск
и показывает ОБЕ шкалы: косинус e5 (0..1) и логит cross-encoder (-11..0).
Затем проверяет сетку порогов ОТДЕЛЬНО по категориям и считает две ошибки:

  промах  — ожидаемый чанк (_expected.kb_ids) не прошёл порог;
  протечка — на rev_004 хоть что-то прошло порог.

Пороги калибруются раздельно, потому что цена ошибки разная:
  complaint — лишний чанк дорого стоит (агент пообещает не то) -> строгий;
  question  — потерять факт дорого, лишний факт бесплатен      -> мягкий.

rev_004 — главная фикстура: в базе знаний нет решения для двойного списания,
и правильный результат для неё — ПУСТОЙ список.

Скрипт ничего не меняет — только читает и печатает.
"""

import json

from reviews_agent.config import PROJECT_ROOT, get_settings
from reviews_agent.db import close_pool
from reviews_agent.rerank import RERANK_THRESHOLDS
from reviews_agent.nodes.retrieve import TOP_K, VECTOR_CANDIDATES, search_facts

REVIEWS_FILE = PROJECT_ROOT / "data" / "reviews.json"
KB_FILE = PROJECT_ROOT / "data" / "knowledge_base.json"

# Категории, которые вообще ходят в Retrieve (praise и spam его минуют).
SEARCHABLE = ("complaint", "question")

# Сетка порогов реранкера. Диапазон взят из замеров: нужные чанки набирают
# от -1.9 до -7.4, шум — от -3.0 до -10.2.
THRESHOLDS = (-1.0, -2.0, -2.25, -2.5, -3.0, -4.0, -6.0, -8.0, -9.0, -11.0)

# Порог заведомо ниже любого реального скора: отключает отсечение,
# чтобы увидеть всех кандидатов.
NO_THRESHOLD = -999.0


def load_data() -> tuple[list[dict], dict[str, str]]:
    """Читает фикстуры и карту 'текст чанка -> его id' для узнавания чанков."""
    with REVIEWS_FILE.open(encoding="utf-8") as f:
        reviews = json.load(f)

    with KB_FILE.open(encoding="utf-8") as f:
        chunks = json.load(f)

    # Ищем по content: в базе id автоинкрементные и меняются при переиндексации,
    # а стабильные kb_001... живут только в JSON.
    content_to_id = {chunk["content"]: chunk["id"] for chunk in chunks}
    return reviews, content_to_id


def collect(reviews: list[dict]) -> dict[str, list[dict]]:
    """
    Прогоняет поиск один раз на фикстуру без отсечения по порогу.

    Результат кэшируется: поиск для всех порогов одинаков, меняется только
    отсечение. Иначе мы бы гоняли векторизацию и реранкер по разу на каждый
    порог из сетки.

    top_k здесь равен VECTOR_CANDIDATES, а не TOP_K: калибровке нужны все
    кандидаты, включая те, что не попали бы в итоговую четвёрку.
    """
    client_id = get_settings().client_id

    return {
        review["review_id"]: search_facts(
            query_text=review["text"],
            client_id=client_id,
            category=review["_expected"]["category"],
            top_k=VECTOR_CANDIDATES,
            threshold=NO_THRESHOLD,
        )
        for review in reviews
        if review["_expected"]["category"] in SEARCHABLE
    }


def show_candidates(
    reviews: list[dict], cache: dict[str, list[dict]], content_to_id: dict[str, str]
) -> None:
    """Печатает всех кандидатов с обеими шкалами."""
    print("=" * 78)
    print(f"КАНДИДАТЫ (вектор top-{VECTOR_CANDIDATES} -> реранкер, без порога)")
    print("=" * 78)

    for review in reviews:
        expected = review["_expected"]
        if expected["category"] not in SEARCHABLE:
            continue

        wanted = expected["kb_ids"]
        print(f"\n{review['review_id']} ({expected['category']}, {expected['language']})")
        print(f"  ожидаются: {wanted if wanted else 'НИЧЕГО — решения в базе нет'}")
        print(f"  {'':<3}{'rerank':>8}{'vector':>9}  чанк")

        for chunk in cache[review["review_id"]]:
            chunk_id = content_to_id.get(chunk["content"], "?")
            mark = "+" if chunk_id in wanted else "-"
            print(
                f"  {mark} {chunk['rerank_score']:>8.3f}{chunk['vector_score']:>9.4f}  "
                f"{chunk_id:<8} {chunk['type']:<7} {chunk['language']:<3} "
                f"{chunk['content'][:40]}"
            )


def test_thresholds(
    reviews: list[dict],
    cache: dict[str, list[dict]],
    content_to_id: dict[str, str],
    category: str,
) -> None:
    """Считает промахи и протечки по каждому порогу для одной категории."""
    subset = [r for r in reviews if r["_expected"]["category"] == category]

    print("\n" + "=" * 78)
    print(f"ПОРОГИ ДЛЯ КАТЕГОРИИ: {category}  (сейчас в коде: {RERANK_THRESHOLDS[category]})")
    print("=" * 78)
    print(f"\n{'порог':<9}{'промахи':<26}{'протечка rev_004':<20}{'вердикт'}")
    print("-" * 78)

    for threshold in THRESHOLDS:
        misses: list[str] = []
        leak_count = 0

        for review in subset:
            expected = review["_expected"]
            passed = [
                c for c in cache[review["review_id"]] if c["rerank_score"] >= threshold
            ]
            # Учитываем реальное отсечение по TOP_K: чанк ниже четвёртого
            # в контекст не попадёт, даже если прошёл порог.
            passed = sorted(passed, key=lambda c: -c["rerank_score"])[:TOP_K]
            passed_ids = {content_to_id.get(c["content"], "?") for c in passed}

            for wanted_id in expected["kb_ids"]:
                if wanted_id not in passed_ids:
                    misses.append(f"{review['review_id']}:{wanted_id}")

            if not expected["kb_ids"] and passed:
                leak_count = len(passed)

        verdict = "OK" if not misses and not leak_count else "плохо"
        misses_str = ", ".join(misses) if misses else "нет"
        leak_str = f"{leak_count} чанк(ов)" if leak_count else "нет"

        print(f"{threshold:<9.2f}{misses_str:<26}{leak_str:<20}{verdict}")


def main() -> None:
    reviews, content_to_id = load_data()
    cache = collect(reviews)

    show_candidates(reviews, cache, content_to_id)

    for category in SEARCHABLE:
        test_thresholds(reviews, cache, content_to_id, category)

    print(
        "\nПромах — ожидаемый чанк не прошёл порог (агент ответит без факта).\n"
        "Протечка — на rev_004 прошёл нерелевантный чанк (агент пообещает не то).\n"
        "Для complaint нужен максимальный порог без промахов и без протечки.\n"
        "Для question протечка неприменима — нужен максимальный порог без промахов."
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()