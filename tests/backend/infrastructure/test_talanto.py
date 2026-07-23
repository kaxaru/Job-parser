"""Тесты источника talanto: ACL talanto-API -> VacancyRecord (домен напрямую, без сети)."""
import pytest

from hrwork.infrastructure import sources
from hrwork.infrastructure.sources.talanto import _meta_header, _normalize, _sig

ITEM = {
    "id": "8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875",
    "title": "Программист Python", "company": "А7-агент (ПСБ)",
    "location": "Москва (м. Киевская)", "remote_type": "office",
    "level": "mid", "employment_type": "full",
    "salary_min": 300000, "salary_max": 400000, "salary_currency": "RUB",
    "published_at": "2026-07-22T18:13:56Z",
    "skills": ["crm", "Python", "API", "Базы данных"],
    "last_verified_at": "2026-07-22T18:13:57.360467Z",
}


def test_normalize_maps_to_domain():
    r = _normalize(ITEM)
    v = r.vacancy
    assert v.id == "talanto_8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875"   # неймспейс
    assert v.source == "talanto"
    assert r.url == "https://talanto.work/jobs/8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875"
    assert v.employer == "А7-агент (ПСБ)"
    assert v.city == "Москва (м. Киевская)"
    assert v.schedule.hh_code == "fullDay"                 # office -> OFFICE
    assert (v.salary.frm, v.salary.to, v.salary.currency) == (300000, 400000, "RUB")
    assert v.experience.hh_id == "between1And3"            # mid
    # skills подмешаны в detect_text/requirement -> детект стека сработал в адаптере
    assert "Python" in v.techs and v.role.is_it
    assert r.sig == ITEM["last_verified_at"]


@pytest.mark.parametrize("remote_type, hh_code", [
    ("remote", "remote"), ("hybrid", "flexible"), ("office", "fullDay"),
    (None, "fullDay"), ("weird", "fullDay"),               # неизвестное -> дефолт OFFICE
])
def test_normalize_schedule_mapping(remote_type, hh_code):
    it = {**ITEM, "remote_type": remote_type}
    assert _normalize(it).vacancy.schedule.hh_code == hh_code


@pytest.mark.parametrize("level, exp", [
    ("junior", "noExperience"), ("mid", "between1And3"),
    ("senior", "between3And6"), ("lead", "moreThan6"),
    (None, None), ("weird", None),                         # мягкий контракт: неизвестное -> None
])
def test_normalize_level_mapping(level, exp):
    v = _normalize({**ITEM, "level": level}).vacancy
    assert (v.experience.hh_id if v.experience else None) == exp


def test_normalize_no_salary_is_none():
    it = {**ITEM, "salary_min": None, "salary_max": None}
    assert _normalize(it).vacancy.salary is None


def test_normalize_enrich_marks_and_meta():
    full = {"description": "<p>Обязанности…</p>", "url": "https://telegram.me/jobs1c/36305"}
    r = _normalize(ITEM, full)
    assert r.enriched and r.enriched_at
    assert "Обязанности" in r.description_html
    assert "telegram.me/jobs1c" in r.description_html      # первоисточник в мета-шапке
    r2 = _normalize(ITEM)                                  # без карточки — не enriched
    assert not r2.enriched and r2.enriched_at is None


def test_normalize_cached_desc_skips_network_flags():
    r = _normalize(ITEM, cached_desc="<p>из кеша</p>", enriched_at="2026-07-20T00:00:00")
    assert r.enriched and r.description_html == "<p>из кеша</p>"
    assert r.enriched_at == "2026-07-20T00:00:00"          # метка реального фетча переносится


def test_sig_falls_back_to_published():
    assert _sig({"published_at": "2026-07-01"}) == "2026-07-01"
    assert _sig({}) == ""


def test_meta_header_escapes_html():
    hdr = _meta_header({"level": "<b>x</b>"}, None)
    assert "<b>" not in hdr                                # чужой текст экранирован


@pytest.mark.parametrize("loc, expected", [
    ("Anywhere in the World, 🇦🇩 Andorra, 🇦🇱 Albania", "Anywhere in the World"),
    ("Remote, Afghanistan, Albania, Algeria", "Remote"),
    ("Москва (м. Киевская)", "Москва (м. Киевская)"),   # настоящий город цел
    (None, ""),
])
def test_clean_city_collapses_country_lists(loc, expected):
    from hrwork.infrastructure.sources.talanto import _clean_city
    assert _clean_city(loc) == expected


def test_clean_city_caps_length():
    from hrwork.infrastructure.sources.talanto import _clean_city
    assert len(_clean_city("X" * 500)) == 80        # мегастрока без запятых — режется по длине


def test_normalize_city_cleaned():
    r = _normalize({**ITEM, "location": "Remote, Argentina, Bolivia, Brazil, Chile"})
    assert r.vacancy.city == "Remote"


def test_sources_registry_has_talanto():
    assert sources.get_source("talanto") is not None
