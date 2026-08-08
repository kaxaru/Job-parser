"""Тесты сопроводительного письма: шаблон (без сети) + fallback LLM->шаблон.

Письмо считается на ДЕФОЛТНОМ шаблоне: `cover.py::COVER_TEMPLATE` печётся на импорте из
`resume_profile.json::cover_template`, и у владельца со своим текстом эти тесты проверяли бы
его предпочтения вместо контракта (аудит 08.08.2026, находка 57). Дефолт возвращает фикстура
`apply_defaults`; доказательство — tests/backend/test_profile_isolation.py.
"""
import pytest

from hrwork.application.apply import cover
from hrwork.application.apply.candidates import Candidate

pytestmark = pytest.mark.usefixtures("apply_defaults")


def test_template_cover_fills_name_and_employer():
    assert cover.template_cover("Python разработчик", "Сбер. IT") == (
        "Здравствуйте! Заинтересовала вакансия Python разработчик в компании Сбер. IT. "
        "Буду рад обсудить детали.")


def test_template_cover_without_employer_names_the_company_generically():
    """Пустой работодатель -> подстановка «вашей компании».

    Текст ЦЕЛИКОМ, а не `"вашей компании" in t`: проверка вхождения прошла бы и на письме,
    где потерялось название вакансии. ЗАМЕЧЕНО 08.08.2026: дефолтный шаблон при этом даёт
    «в компании вашей компании» — заявка владельцу `cover.py` подана отдельно, а литерал
    здесь не даёт правке текста пройти молча."""
    assert cover.template_cover("Backend инженер", "") == (
        "Здравствуйте! Заинтересовала вакансия Backend инженер в компании вашей компании. "
        "Буду рад обсудить детали.")


def test_build_cover_default_is_template():
    # ЛИТЕРАЛ, а не `== cover.template_cover("Data Engineer", "Ozon")` (находка 62): сломанный
    # `template_cover` ломал бы обе стороны равенства одинаково, и тест прошёл бы на любом
    # тексте, включая пустой.
    c = Candidate(id="1", name="Data Engineer", url="", employer="Ozon")
    assert cover.build_cover(c) == (
        "Здравствуйте! Заинтересовала вакансия Data Engineer в компании Ozon. "
        "Буду рад обсудить детали.")


def test_build_cover_llm_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = Candidate(id="1", name="QA", url="", employer="X", desc="тест")
    assert cover.build_cover(c, mode="llm") == (
        "Здравствуйте! Заинтересовала вакансия QA в компании X. Буду рад обсудить детали.")
