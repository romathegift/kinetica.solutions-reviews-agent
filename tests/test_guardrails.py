"""
Тесты гардрейлов. Единственная цель файла — чтобы мои дефекты ловила машина,
а не Роман глазами в превью (каждый такой цикл оплачен Sonnet'ом).

Принципы, по которым написаны тесты:

  * БЕЗ сети, модели и БД. check_draft и все функции ниже — чистая арифметика
    и сравнение строк. Единственная внешняя зависимость на импорте —
    cyrillic_ratio из nodes/ingest.py (чистая функция) и константы бренда
    из brand.py.

  * Списки фраз НЕ хардкодятся. CLICHES / ACTION_PHRASES / SIGNATURES
    импортируются из brand.py, и позитивные кейсы параметризуются реальными
    списками. Тест не разъедется с брендом, когда тот изменится.

  * Две регрессии прошлой сессии зашиты отдельными именованными тестами:
      - 🐾 на тёплом нейтральном вопросе НЕ даёт EMOJI_FORBIDDEN;
      - «повернемось до вас з відповіддю» на жалобе НЕ даёт
        PROMISE_REQUIRES_ACTION (голое «повернемо» больше не матчится).
    Вторая — та самая, что модель дважды просемплила мимо; теперь её
    доказывает константа, а не прогон.

  * Где проверка зависит от содержимого brand.py, которого тест не видит,
    утверждение делается через `in` / `not in` для КОНКРЕТНОГО флага,
    а не через равенство всего списка. Цель — чтобы тест не падал из-за
    незнания точного вида бренда, а падал только на реальном дефекте.

Запуск из корня репозитория:  python -m pytest tests/ -v
(`python -m` кладёт cwd в sys.path — reviews_agent импортируется без pyproject.)
"""

import pytest

from reviews_agent.brand import ACTION_PHRASES, CLICHES, SIGNATURES
from reviews_agent.guardrails import (
    ALL_FLAGS,
    CLICHE,
    CONTEXT_INCOMPLETE,
    EMPTY_DRAFT,
    EMOJI_FORBIDDEN,
    EMOJI_TOO_MANY,
    LANG_MISMATCH,
    LENGTH_OUT_OF_RANGE,
    PII_IN_REPLY,
    PROMISE_REQUIRES_ACTION,
    SIGNATURE_MISSING,
    TRUNCATED,
    check_draft,
    count_emoji,
    count_sentences,
    emoji_limit,
    has_pii,
    normalize,
    strip_signature,
)

# --- Хелперы и константы тестов ----------------------------------------------

PAW = "\U0001F43E"      # 🐾 U+1F43E PAW PRINTS — категория So
SMILE = "\U0001F642"    # 🙂 U+1F642 SLIGHTLY SMILING FACE — категория So
# 👨‍👩‍👧: три символа So, склеенные двумя ZWJ (U+200D, категория Cf).
# По устройству count_emoji это ТРИ эмодзи — задокументированный предел.
FAMILY_ZWJ = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
# ^ (U+005E) и ` (U+0060) — категория Sk, НЕ So. count_emoji их не считает.
SK_SYMBOLS = "^`"


def _is_subsequence(sub, full):
    """True, если sub встречается в full как подпоследовательность (порядок важен)."""
    it = iter(full)
    return all(item in it for item in sub)


# Пары (язык, фраза) из РЕАЛЬНЫХ списков бренда — источник для параметризации.
# Пустой список для языка даёт ноль кейсов, а не падение.
ACTION_CASES = [(lang, phrase) for lang in ("uk", "en") for phrase in ACTION_PHRASES[lang]]
CLICHE_CASES = [(lang, phrase) for lang in ("uk", "en") for phrase in CLICHES[lang]]


# =============================================================================
# Чистые функции
# =============================================================================

# --- normalize ---------------------------------------------------------------

def test_normalize_casefolds():
    assert normalize("ПРИВІТ Світ") == "привіт світ"


def test_normalize_strips_quotes_to_space():
    # Все кавычки из QUOTE_CHARS превращаются в пробел и схлопываются:
    # «ялинки», прямые и низкие — одна и та же подпись, флаг за них был бы ложным.
    assert normalize("«Медовий Ранок»") == "медовий ранок"
    assert normalize('«Медовий» "Ранок"') == "медовий ранок"


def test_normalize_collapses_whitespace():
    assert normalize("а   б\t\nв") == "а б в"


def test_normalize_keeps_apostrophe():
    # ' (U+0027) и ’ (U+2019) — буква по функции в украинском, не кавычка.
    # Снятие разорвало бы слово пополам, поэтому их в QUOTE_CHARS нет.
    assert normalize("здоров'я") == "здоров'я"
    assert normalize("обов\u2019язково") == "обов\u2019язково"


# --- count_sentences ---------------------------------------------------------

def test_count_sentences_basic():
    assert count_sentences("Дякуємо") == 1          # без точки — одно, не ноль
    assert count_sentences("Привіт. Як справи?") == 2
    assert count_sentences("Раз. Два. Три. Чотири.") == 4


def test_count_sentences_empty_is_zero():
    assert count_sentences("") == 0
    assert count_sentences("   ") == 0


def test_count_sentences_decimal_not_split():
    # Требование пробела после точки защищает дробные числа от разрыва.
    assert count_sentences("Це число 1.5 літра.") == 1
    assert count_sentences("1.5 л") == 1


def test_count_sentences_ellipsis_recognised():
    # U+2026 включён в терминальную пунктуацию явно.
    assert count_sentences("Отже\u2026") == 1


# --- count_emoji -------------------------------------------------------------

def test_count_emoji_counts_so():
    assert count_emoji(PAW) == 1
    assert count_emoji(PAW + SMILE) == 2
    assert count_emoji("Текст без емодзі") == 0


def test_count_emoji_ignores_sk_modifiers():
    # Категория Sk (^, `, модификаторы тона) НЕ берётся: иначе кавычка-циркумфлекс
    # давала бы ложный флаг. Осознанный размен из докстринга.
    assert count_emoji(SK_SYMBOLS) == 0


def test_count_emoji_zwj_sequence_known_limitation():
    # 👨‍👩‍👧 считается как ТРИ (три So); ZWJ не в счёт. Задокументированный предел.
    assert count_emoji(FAMILY_ZWJ) == 3


# --- emoji_limit -------------------------------------------------------------

def test_emoji_limit_complaint_is_zero():
    assert emoji_limit("complaint", "positive") == 0
    assert emoji_limit("complaint", "neutral") == 0
    assert emoji_limit("complaint", "negative") == 0


def test_emoji_limit_negative_is_zero():
    # Негативный отзыв, не попавший в complaint (разочарованный вопрос), — тоже 0.
    assert emoji_limit("question", "negative") == 0


def test_emoji_limit_positive_context_is_one():
    assert emoji_limit("question", "neutral") == 1
    assert emoji_limit("question", "positive") == 1


# --- has_pii -----------------------------------------------------------------

def test_has_pii_email():
    assert has_pii("Пишіть на test@example.com") is True


def test_has_pii_phone():
    assert has_pii("Телефон +380 44 123 45 67") is True


def test_has_pii_card_number():
    assert has_pii("Картка 4111 1111 1111 1111") is True


def test_has_pii_short_number_is_not_pii():
    # «понад 15 хвилин» из kb_009: короткое число — легитимный факт, не контакт.
    assert has_pii("Зачекайте понад 15 хвилин") is False


def test_has_pii_digit_count_guard():
    # Форма телефона есть (regex матчится), но цифр 8 (<9) — не контакт.
    # Проверяет вторую ступень: сначала форма, потом счёт цифр.
    assert has_pii("1234 5678") is False


def test_has_pii_opening_hours_not_pii():
    # «8:00-21:00» из kb_001: двоеточие намеренно вне класса телефона.
    assert has_pii("Працюємо 8:00-21:00") is False


# --- strip_signature (принимает УЖЕ нормализованный черновик) -----------------

def test_strip_signature_present():
    normalized = normalize("Дякуємо за візит. " + SIGNATURES["uk"])
    body, present = strip_signature(normalized, "uk")
    assert present is True
    assert not body.endswith(normalize(SIGNATURES["uk"]))


def test_strip_signature_absent():
    normalized = normalize("Текст зовсім без підпису бренду")
    body, present = strip_signature(normalized, "uk")
    assert present is False
    assert body == normalized


def test_strip_signature_tolerates_trailing_punct():
    # Точка после подписи не делает её другой: хвостовая пунктуация снимается.
    normalized = normalize("Дякуємо. " + SIGNATURES["uk"] + ".")
    _, present = strip_signature(normalized, "uk")
    assert present is True


def test_strip_signature_english():
    normalized = normalize("Thank you for visiting. " + SIGNATURES["en"])
    body, present = strip_signature(normalized, "en")
    assert present is True
    assert not body.endswith(normalize(SIGNATURES["en"]))


# =============================================================================
# check_draft — единственная точка входа
# =============================================================================

# --- флаги-параметры (TRUNCATED, CONTEXT_INCOMPLETE) -------------------------

def test_truncated_flag_from_param():
    flags = check_draft("Будь-який непорожній текст.", "uk", "question", "neutral", truncated=True)
    assert TRUNCATED in flags


def test_context_incomplete_flag_from_param():
    flags = check_draft(
        "Будь-який непорожній текст.", "uk", "question", "neutral", context_incomplete=True
    )
    assert CONTEXT_INCOMPLETE in flags


def test_param_flags_absent_by_default():
    flags = check_draft("Будь-який непорожній текст. " + SIGNATURES["uk"], "uk", "question", "neutral")
    assert TRUNCATED not in flags
    assert CONTEXT_INCOMPLETE not in flags


# --- EMPTY_DRAFT и ранний выход ----------------------------------------------

def test_empty_draft_short_circuits():
    # На пустой строке — ровно один флаг, без шумовых SIGNATURE_MISSING и т.д.
    assert check_draft("   ", "uk", "question", "neutral") == [EMPTY_DRAFT]


def test_empty_draft_keeps_only_param_flags():
    # Флаги-параметры доезжают, но текстовые проверки не запускаются.
    flags = check_draft("", "uk", "question", "neutral", truncated=True)
    assert flags == [TRUNCATED, EMPTY_DRAFT]


# --- LANG_MISMATCH -----------------------------------------------------------

def test_lang_mismatch_latin_in_uk():
    flags = check_draft("This whole reply is in English text.", "uk", "question", "neutral")
    assert LANG_MISMATCH in flags


def test_lang_mismatch_cyrillic_in_en():
    flags = check_draft("Ця відповідь повністю кирилицею.", "en", "question", "neutral")
    assert LANG_MISMATCH in flags


def test_no_lang_mismatch_clean_uk():
    flags = check_draft("Так, ми відкриті щодня. " + SIGNATURES["uk"], "uk", "question", "neutral")
    assert LANG_MISMATCH not in flags


def test_no_lang_mismatch_clean_en():
    flags = check_draft("Yes, we are open every day. " + SIGNATURES["en"], "en", "question", "neutral")
    assert LANG_MISMATCH not in flags


# --- LENGTH_OUT_OF_RANGE -----------------------------------------------------

def test_length_too_many_sentences():
    draft = "Раз. Два. Три. Чотири. П'ять. " + SIGNATURES["uk"]
    flags = check_draft(draft, "uk", "question", "neutral")
    assert LENGTH_OUT_OF_RANGE in flags


def test_length_signature_only_body_is_empty():
    # Тело — только подпись: предложений ноль, диапазон это ловит.
    flags = check_draft(SIGNATURES["uk"], "uk", "question", "neutral")
    assert LENGTH_OUT_OF_RANGE in flags


def test_length_ok_four_sentences_plus_signature():
    # Четыре фразы + подпись НЕ должны упираться в потолок: подпись срезается
    # перед счётом. Без среза было бы пять — ложный флаг на нормальном ответе.
    draft = "Раз. Два. Три. Чотири. " + SIGNATURES["uk"]
    flags = check_draft(draft, "uk", "question", "neutral")
    assert LENGTH_OUT_OF_RANGE not in flags


# --- SIGNATURE_MISSING -------------------------------------------------------

def test_signature_missing():
    flags = check_draft("Дякуємо за ваш візит до нас.", "uk", "question", "neutral")
    assert SIGNATURE_MISSING in flags


def test_signature_present():
    flags = check_draft("Дякуємо за ваш візит. " + SIGNATURES["uk"], "uk", "question", "neutral")
    assert SIGNATURE_MISSING not in flags


# --- CLICHE (позитивы параметризованы реальным списком) ----------------------

@pytest.mark.parametrize("language,phrase", CLICHE_CASES)
def test_cliche_phrase_flags(language, phrase):
    # Каждое клише из brand.py, будучи в черновике, обязано поднять CLICHE.
    draft = phrase + " " + SIGNATURES[language]
    flags = check_draft(draft, language, "question", "neutral")
    assert CLICHE in flags


# --- PROMISE_REQUIRES_ACTION (позитивы + регрессия «повернемо») ---------------

@pytest.mark.parametrize("language,phrase", ACTION_CASES)
def test_action_phrase_flags(language, phrase):
    # Каждая фраза-обещание из brand.py обязана поднять PROMISE_REQUIRES_ACTION.
    # Категория тут не важна: проверка идёт по ТЕКСТУ, а не по типу отзыва.
    draft = phrase + " " + SIGNATURES[language]
    flags = check_draft(draft, language, "question", "neutral")
    assert PROMISE_REQUIRES_ACTION in flags


def test_promise_regression_povernemos_reply_not_action():
    # РЕГРЕССИЯ. «повернемось до вас з відповіддю» — обещание ОТВЕТИТЬ (kb_014
    # требует его на каждой жалобе), а не вернуть деньги. Голое «повернемо» как
    # подстрока матчило «повернемо**сь**» — ложный PROMISE. Фикс в brand.py:
    # «повернемо» стоит только с объектом («повернемо кошти/гроші/вартість»).
    # Эта константа — и есть доказательство фикса, которого не было прогоном.
    draft = "Ми повернемось до вас з відповіддю найближчим часом. " + SIGNATURES["uk"]
    flags = check_draft(draft, "uk", "complaint", "negative")
    assert PROMISE_REQUIRES_ACTION not in flags


# --- эмодзи (в т.ч. регрессия 🐾) ---------------------------------------------

def test_emoji_forbidden_on_complaint():
    # Жалоба → лимит 0. Любой эмодзи запрещён.
    draft = "Дуже шкода, що так вийшло. " + SIGNATURES["uk"] + " " + PAW
    flags = check_draft(draft, "uk", "complaint", "negative")
    assert EMOJI_FORBIDDEN in flags


def test_emoji_regression_paw_on_warm_neutral_question():
    # РЕГРЕССИЯ rev_005. Тёплый ответ на нейтральный вопрос с одним 🐾.
    # Строгое чтение «позитивный контекст = sentiment positive» давало здесь
    # ложный EMOJI_FORBIDDEN. Правило запрещает эмодзи на ЖАЛОБАХ; нейтральный
    # вопрос, отвеченный тепло, — позитивный контекст. Лимит 1, эмодзи 1 → чисто.
    draft = "Так, ми вас чекаємо разом із песиком, принесемо і миску води. " + SIGNATURES["uk"] + " " + PAW
    flags = check_draft(draft, "uk", "question", "neutral")
    assert EMOJI_FORBIDDEN not in flags
    assert EMOJI_TOO_MANY not in flags


def test_emoji_too_many_over_limit():
    # Позитивный контекст → лимит 1. Два эмодзи превышают.
    draft = "Дуже раді вас бачити знову! " + SIGNATURES["uk"] + " " + PAW + SMILE
    flags = check_draft(draft, "uk", "question", "positive")
    assert EMOJI_TOO_MANY in flags
    assert EMOJI_FORBIDDEN not in flags


# --- PII_IN_REPLY ------------------------------------------------------------

def test_pii_in_reply_phone():
    draft = "Зателефонуйте на +380 44 123 45 67, будь ласка. " + SIGNATURES["uk"]
    flags = check_draft(draft, "uk", "question", "neutral")
    assert PII_IN_REPLY in flags


# --- структурные инварианты --------------------------------------------------

def test_flags_follow_all_flags_order_no_duplicates():
    # Что бы ни сработало — порядок совпадает с ALL_FLAGS, дублей нет.
    draft = (
        "Call +1 234 567 8900 today. Reply is English but tagged uk. "
        "Sentence three. Sentence four. Sentence five."
    )
    flags = check_draft(
        draft, "uk", "complaint", "negative", truncated=True, context_incomplete=True
    )
    assert _is_subsequence(flags, ALL_FLAGS)
    assert len(flags) == len(set(flags))


def test_all_flags_contains_every_code():
    # Полнота реестра: слой tg/ по ALL_FLAGS проверяет, что у кода есть текст.
    # Добавили флаг, но забыли в ALL_FLAGS (или наоборот) — падёт здесь.
    expected = {
        TRUNCATED,
        CONTEXT_INCOMPLETE,
        EMPTY_DRAFT,
        LANG_MISMATCH,
        LENGTH_OUT_OF_RANGE,
        SIGNATURE_MISSING,
        CLICHE,
        EMOJI_FORBIDDEN,
        EMOJI_TOO_MANY,
        PII_IN_REPLY,
        PROMISE_REQUIRES_ACTION,
    }
    assert set(ALL_FLAGS) == expected
    assert len(ALL_FLAGS) == len(expected)  # без дублей и без лишних


# --- «чистый» черновик не поднимает ДЕТЕРМИНИРОВАННЫХ флагов ------------------

# CLICHE и PROMISE_REQUIRES_ACTION исключены из проверки намеренно: они зависят
# от содержимого brand.py, которого тест не видит, поэтому допускаются в наборе.
# Если сработает что-то ещё — это дефект машинерии, и тест обязан упасть.
_BRAND_DEPENDENT = {CLICHE, PROMISE_REQUIRES_ACTION}


def test_clean_uk_draft_no_deterministic_flags():
    # Латиница Wi-Fi легитимна (kb_005) и не должна ронять LANG_MISMATCH.
    draft = "Так, у нас є безкоштовний Wi-Fi для гостей. " + SIGNATURES["uk"]
    assert set(check_draft(draft, "uk", "question", "neutral")) <= _BRAND_DEPENDENT


def test_clean_en_draft_no_deterministic_flags():
    draft = "Yes, we offer free Wi-Fi for all our guests. " + SIGNATURES["en"]
    assert set(check_draft(draft, "en", "question", "neutral")) <= _BRAND_DEPENDENT
