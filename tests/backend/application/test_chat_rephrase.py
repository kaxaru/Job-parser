"""LLM-переформулировка (etap 2). Главное свойство: валидатор _grounded режет ЛЮБОЙ
ново-токенный факт, а при любом сбое/reject возвращается ИСТОЧНИК дословно. Транспорт
замокан. Перестановку клауз валидатор НЕ ловит намеренно — её держит human-gate (см.
test_chat_reply.py::test_run_rephrase_*)."""
import os

import pytest

from hrwork.application.apply.chat import chat_rephrase as R


# ── eligible: строго default-DENY allow-list ──
@pytest.mark.parametrize("rule,ok", [
    ("years", True), ("frontend", True), ("stack_list", True),
    ("has_exp_yes", True), ("has_exp_past", True), ("format", True),
    ("english", True), ("education", True), ("citizenship", True),
    ("practice_ml", True), ("practice_testing", True),
    ("salary_junior", False), ("place_own_city", False), ("place_remote", False),
    ("confirm", False), ("has_exp_no", False),
    ("brand_new_future_rule", False), ("", False),
])
def test_eligible_default_deny(rule, ok):
    assert R.eligible(rule) is ok


# ── _grounded: reorder/перефраз без новых токенов -> True ──
@pytest.mark.parametrize("source,candidate,lang", [
    ("Автоматизирую тестирование на Python и Selenium.",
     "На Python и Selenium автоматизирую тестирование.", "ru"),
    ("I write tests with Pytest and Selenium.",
     "With Pytest and Selenium I write tests.", "en"),
    ("I use Pytest and Selenium.", "I also use Pytest and Selenium.", "en"),  # добавлено служебное
])
def test_grounded_accepts_pure_reorder(source, candidate, lang):
    assert R._grounded(source, candidate, lang) is True


def test_grounded_accepts_identical():
    assert R._grounded("Английский B1 (средний).", "Английский B1 (средний).", "ru") is True


@pytest.mark.parametrize("candidate", ["", "   ", "\n\t"])
def test_grounded_empty_rejected(candidate):
    assert R._grounded("Опыт 5 лет.", candidate, "ru") is False


# ── _grounded: любой НОВЫЙ значимый токен -> False ──
@pytest.mark.parametrize("source,candidate,lang", [
    ("Опыт 5 лет.", "Опыт 7 лет.", "ru"),                        # новое число
    ("Пишу на Python.", "Пишу на Python и Kafka.", "ru"),        # новая технология (кириллица-контекст)
    ("I use Pytest and Selenium.", "I use Pytest, Selenium and Kubernetes.", "en"),  # новая тех EN
    ("Работал с Docker.", "Пять лет работал с Docker.", "ru"),   # прописью-число
    ("Стаж 3 года.", "Стаж ５ года.", "ru"),                  # full-width 5 -> NFKC -> 5 (новое число)
])
def test_grounded_rejects_new_token(source, candidate, lang):
    assert R._grounded(source, candidate, lang) is False


# ── C3-ослабление: русская морфология источника проходит, чужие токены — нет ──
@pytest.mark.parametrize("source,candidate", [
    ("Я работал с Docker и Kafka.", "Я работаю с Docker и Kafka."),                    # работал -> работаю
    ("Коммерческий опыт на Python — 3 года.", "Коммерческого опыта на Python — 3 года."),
])
def test_grounded_accepts_ru_morphology(source, candidate):
    assert R._grounded(source, candidate, "ru") is True


def test_grounded_stem_rejects_new_cyrillic_word():
    # новое кириллическое СЛОВО без корня в источнике -> отклонить (не морфология)
    assert R._grounded("Настраивал мониторинг.", "Настраивал нагрузочное тестирование.", "ru") is False


def test_grounded_stem_keeps_numbers_and_tech_strict():
    # ослабление НЕ трогает цифры и латиницу: новый факт там всегда режется
    assert R._grounded("Опыт на Python — 3 года.", "Опыт на Python — 5 лет.", "ru") is False   # 5 нов.
    assert R._grounded("Пишу на Python.", "Работаю на Python и Kafka.", "ru") is False          # Kafka нов.


def test_grounded_stem_rejects_prefix_collision():
    # «работал» и «работник» делят корень «работ», но это РАЗНЫЕ слова -> порог 0.7 отклоняет
    assert R._grounded("Я работал с Docker.", "Я работник с Docker.", "ru") is False


def test_grounded_over_length_rejected():
    src = "Да."
    assert R._grounded(src, "Да, " + "очень " * 40, "ru") is False


@pytest.mark.parametrize("source,candidate,lang", [
    ("На Python — 3 года.", "3 years of Python.", "ru"),         # RU-ответ, латиница -> False
    ("Yes, B1 level.", "Да, уровень B1.", "en"),                 # EN-ответ, кириллица -> False
])
def test_grounded_language_drift_rejected(source, candidate, lang):
    assert R._grounded(source, candidate, lang) is False


# ── _grounded: отрицания — строгое равенство (C4) ──
def test_grounded_negation_flip_rejected():
    assert R._grounded("Нет, с этим не работал.", "Да, с этим работал.", "ru") is False


def test_grounded_negation_insert_rejected():
    assert R._grounded("Да, есть опыт с Docker.", "Нет, нет опыта с Docker.", "ru") is False


def test_grounded_question_echo_rejected():
    # эхо вопроса разделяет мало токенов с ответом -> C5 порог перекрытия
    src = "Автоматизирую тестирование на Python и Selenium."
    assert R._grounded(src, "А расскажите про ваш опыт подробнее.", "ru") is False


# ── rephrase_answer: оркестрация + fallback-safe (транспорт замокан) ──
def _transport(monkeypatch, ret):
    monkeypatch.setattr(R, "chat_json", lambda s, u, **k: ret)


def test_excluded_rule_returns_source_no_network(monkeypatch):
    called = []
    monkeypatch.setattr(R, "chat_json", lambda *a, **k: called.append(1))
    out = R.rephrase_answer("Зарплата?", "150–170к", "salary_junior", "ru")
    assert out == "150–170к" and called == []          # не eligible -> сеть НЕ зовётся


def test_no_key_returns_source(monkeypatch):
    _transport(monkeypatch, None)                       # chat_json -> None (нет ключа/сеть)
    src = "Автоматизирую тестирование на Python и Selenium."
    assert R.rephrase_answer("Про тесты?", src, "practice_testing", "ru") == src


def test_ungrounded_returns_source(monkeypatch):
    _transport(monkeypatch, "Автоматизирую тестирование на Python, Selenium и Kafka.")  # +Kafka
    src = "Автоматизирую тестирование на Python и Selenium."
    assert R.rephrase_answer("Про тесты?", src, "practice_testing", "ru") == src


def test_markdown_output_returns_source(monkeypatch):
    _transport(monkeypatch, "```\nАвтоматизирую тестирование на Python и Selenium.\n```")
    src = "Автоматизирую тестирование на Python и Selenium."
    assert R.rephrase_answer("Про тесты?", src, "practice_testing", "ru") == src


def test_grounded_candidate_returned(monkeypatch):
    cand = "На Python и Selenium автоматизирую тестирование."
    _transport(monkeypatch, cand)
    src = "Автоматизирую тестирование на Python и Selenium."
    assert R.rephrase_answer("Про тесты?", src, "practice_testing", "ru") == cand


# ── guard: _FUNC не содержит числительных/технологий/оценок/ОТРИЦАНИЙ ──
_FORBIDDEN = {
    "один", "два", "три", "пять", "десять", "one", "two", "three", "five", "ten",   # числительные
    "не", "нет", "ни", "без", "никогда", "no", "not", "never", "without",           # отрицания
    "senior", "expert", "fluent", "advanced", "native", "много", "отлично",         # оценки уровня
    "python", "react", "docker", "kafka", "java", "selenium",                       # технологии
}


@pytest.mark.parametrize("lang", list(R._FUNC), ids=str)
def test_func_allowlist_has_no_numerals_tech_or_negation(lang):
    # параметризуемся по ВСЕМ языкам словаря: добавят третий — он проверится сам,
    # а падение назовёт язык в отчёте, вместо «упал первый, про остальные неизвестно»
    assert R._FUNC[lang] & _FORBIDDEN == set()


# ══════════════ live: реальный OpenRouter (opt-in, как test_chat_intent) ══════════════
@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("REPHRASE_LIVE"),
                    reason="сетевой вызов OpenRouter — opt-in: REPHRASE_LIVE=1 pytest")
def test_live_rephrase_stays_grounded():
    src = "Автоматизирую тестирование на Python и Selenium."
    out = R.rephrase_answer("Расскажите про ваш опыт автоматизации?", src, "practice_testing", "ru")
    assert out, "пустой результат"
    # по конструкции: либо источник (fallback), либо кандидат, прошедший _grounded
    assert out == src or R._grounded(src, out, "ru")
