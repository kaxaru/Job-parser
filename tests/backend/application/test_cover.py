"""Тесты сопроводительного письма: шаблон (без сети) + fallback LLM->шаблон."""
from hrwork.application.apply import cover
from hrwork.application.apply.candidates import Candidate


def test_template_cover_fills_name_and_employer():
    t = cover.template_cover("Python разработчик", "Сбер. IT")
    assert t == ("Здравствуйте! Заинтересовала вакансия Python разработчик в компании "
                 "Сбер. IT. Буду рад обсудить детали.")


def test_template_cover_empty_employer():
    t = cover.template_cover("Backend инженер", "")
    assert "вашей компании" in t


def test_build_cover_default_is_template():
    c = Candidate(id="1", name="Data Engineer", url="", employer="Ozon")
    assert cover.build_cover(c) == cover.template_cover("Data Engineer", "Ozon")


def test_build_cover_llm_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = Candidate(id="1", name="QA", url="", employer="X", desc="тест")
    assert cover.build_cover(c, mode="llm") == cover.template_cover("QA", "X")
