"""Тесты источника getmatch: ACL getmatch-API -> VacancyRecord (домен напрямую, без сети)."""
import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.sources.getmatch import _normalize, _sig

ITEM = {
    "id": 35091,
    "position": "Администратор систем сбора событий с конечных точек (EDR)",
    "url": "/vacancies/35091-administrator-sistem-sbora-sobytii",
    "published_at": "2026-08-01T09:00:46.772355",
    "offer_type": "vacancy",
    "company": {"name": "Сбер", "url": "/companies/GNRXrNQz-sber"},
    "location_items": [{"label": "Москва (м. Площадь Ильича)", "format": "office"}],
    "location_requirements": [{"city": "Москва", "country": "Россия", "format": "office"}],
    "skills_objects": [{"name": "PostgreSQL", "slug": "postgresql"},
                       {"name": "Python", "slug": "python"},
                       {"name": "Kafka", "slug": "kafka"}],
    "salary_display_from": 270000, "salary_display_to": 350000,
    "salary_currency": "RUB", "salary_taxes": "gross", "salary_hidden": False,
    "offer_description": "<b>Что делать:</b> развёртывать и настраивать серверы EDR.",
    "description_html": None,
}
FULL = {
    **ITEM,
    "description": "<h2>Задачи</h2>\n<ul><li>развертывание и настройка</li></ul>",
    "seniority": "senior", "seniorities": ["senior"],
    "required_years_of_experience": 3,
}


def test_normalize_maps_to_domain():
    r = _normalize(ITEM, FULL)
    v = r.vacancy
    assert v.id == "getmatch_35091"                     # неймспейс — не столкнётся с id HH
    assert v.source == "getmatch"
    assert v.employer == "Сбер"
    assert v.city == "Москва"                           # из location_requirements, не из label
    assert r.url == "https://getmatch.ru/vacancies/35091-administrator-sistem-sbora-sobytii"
    assert v.schedule.hh_code == "fullDay"
    assert v.experience.hh_id == "between3And6"         # seniority=senior
    assert v.responses is None                          # счётчика откликов API не отдаёт


def test_gross_salary_is_marked_gross_not_recomputed():
    """salary_taxes приходит явно — НДФЛ считает Salary VO, а не адаптер."""
    v = _normalize(ITEM, FULL).vacancy
    assert (v.salary.frm, v.salary.to, v.salary.currency) == (270000, 350000, "RUB")
    assert v.salary.gross is True


def test_hidden_salary_is_none():
    it = {**ITEM, "salary_display_from": None, "salary_display_to": None,
          "salary_currency": None, "salary_hidden": True}
    assert _normalize(it, FULL).vacancy.salary is None


def test_structured_skills_feed_single_detection_point():
    """skills_objects идут в detect_text, а не в обход _detect_techs."""
    v = _normalize(ITEM, FULL).vacancy
    assert "Python" in v.techs and "PostgreSQL" in v.techs
    assert v.role.is_it


def test_full_description_comes_from_card_not_list():
    """В списке description_html всегда null, полный текст — в /api/offers/{id}::description."""
    from_list = _normalize(ITEM)
    from_card = _normalize(ITEM, FULL)
    assert from_list.enriched is False
    assert from_list.description_html == ITEM["offer_description"]
    assert from_card.enriched is True
    assert from_card.description_html == FULL["description"]


def test_grade_absent_without_card():
    """Грейда в списке нет — честный None вместо выдуманного уровня."""
    assert _normalize(ITEM).vacancy.experience is None


def test_sig_tracks_publication_change():
    assert _sig(ITEM) == "2026-08-01T09:00:46.772355"
    assert _sig({**ITEM, "published_at": "2026-08-02T10:00:00"}) != _sig(ITEM)


def test_cached_description_skips_network_path():
    r = _normalize(ITEM, cached_desc="<p>из кеша</p>", enriched_at="2026-07-31T08:00:00Z")
    assert r.description_html == "<p>из кеша</p>"
    assert (r.enriched, r.enriched_at) == (True, "2026-07-31T08:00:00Z")


# ── VO-фабрики getmatch ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seniority,years,expected", [
    ("junior", 1, "between1And3"),
    ("middle", 2, "between1And3"),
    ("senior", 3, "between3And6"),      # грейд важнее лет: senior с years=3 — не junior
    ("lead", 6, "moreThan6"),
    (None, 0, "noExperience"),          # грейда нет -> по годам
    (None, 2, "between1And3"),
    (None, 5, "between3And6"),
    (None, 9, "moreThan6"),
])
def test_experience_from_getmatch(seniority, years, expected):
    assert Experience.from_getmatch(seniority, years).hh_id == expected


def test_experience_unknown_is_none():
    assert Experience.from_getmatch(None, None) is None


@pytest.mark.parametrize("formats,expected", [
    (["remote"], "remote"),
    (["hybrid"], "flexible"),
    (["office"], "fullDay"),
    (["office", "remote"], "remote"),               # приоритет remote > hybrid > офис
    (["office", "hybrid"], "flexible"),
    (["relocation_company"], "fullDay"),            # переезд ради работы В ОФИСЕ
    ([], "fullDay"),
])
def test_schedule_from_getmatch(formats, expected):
    assert Schedule.from_getmatch(formats).hh_code == expected
