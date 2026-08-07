"""Адаптеры глобальных источников: arbeitnow.com и himalayas.app.

Оба портала — ОБЩИЕ job-борды (не IT), англоязычные, с полным описанием прямо в списке.
Здесь проверяется только маппинг внешней схемы в домен (ACL) — сеть не трогается.

Два инварианта, ради которых тесты и написаны (оба — реальные дефекты, пойманные живым
прогоном 07.08.2026, а не выдуманные случаи):
  * himalayas отдаёт в `companyName` строку-ЗАГЛУШКУ "name" во всех записях. Ключ кросс-
    портальной дедупликации — пара (работодатель, тайтл), поэтому взять её означало бы
    схлопнуть разные компании в одну вакансию;
  * зарплата у himalayas ГОДОВАЯ (88 % записей), а весь домен считает вилку месячной.
"""
import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.sources import arbeitnow, himalayas


def _arb(**kw):
    base = {
        "slug": "python-backend-engineer-berlin-12345",
        "title": "Python Backend Engineer",
        "company_name": "Acme GmbH",
        "location": "Berlin",
        "remote": True,
        "job_types": ["professional / experienced"],
        "tags": ["Software Development"],
        "description": "<p>We use Python, FastAPI and PostgreSQL.</p>",
        "url": "https://www.arbeitnow.com/jobs/companies/acme/python-backend-12345",
        "created_at": 1786046437,
    }
    return {**base, **kw}


def _him(**kw):
    base = {
        "guid": "https://himalayas.app/companies/bright-vision/jobs/data-engineer-4189696294",
        "title": "Data Engineer",
        "companyName": "name",                      # заглушка портала — брать нельзя
        "companySlug": "bright-vision-technologies",
        "locationRestrictions": ["United States"],
        "seniority": ["Mid-level"],
        "minSalary": 120000,
        "maxSalary": 180000,
        "currency": "USD",
        "salaryPeriod": "annual",
        "employmentType": "Full Time",
        "excerpt": "Build data pipelines",
        "description": "<p>Python, Airflow, dbt</p>",
        "categories": ["Data Science"],
        "pubDate": 1786043569,
    }
    return {**base, **kw}


# ── arbeitnow ──────────────────────────────────────────────────────────────────

def test_arbeitnow_maps_card_to_domain():
    r = arbeitnow._normalize(_arb())
    v = r.vacancy
    assert v.id == "arbeitnow_python-backend-engineer-berlin-12345"
    assert v.name == "Python Backend Engineer"
    assert v.employer == "Acme GmbH"
    assert v.city == "Berlin"
    assert v.source == "arbeitnow"
    assert v.schedule is Schedule.REMOTE
    assert r.url == "https://www.arbeitnow.com/jobs/companies/acme/python-backend-12345"


@pytest.mark.parametrize("remote, expected", [
    (True, Schedule.REMOTE),
    (False, Schedule.OFFICE),
])
def test_arbeitnow_remote_flag_becomes_schedule(remote, expected):
    assert arbeitnow._normalize(_arb(remote=remote)).vacancy.schedule is expected


def test_arbeitnow_has_no_salary_or_grade():
    # API не отдаёт ни вилку, ни грейд — выдумывать их нельзя, иначе поедут срезы аналитики
    v = arbeitnow._normalize(_arb()).vacancy
    assert v.salary is None
    assert v.experience is None


def test_arbeitnow_unix_timestamp_becomes_iso():
    # 1786046437 -> 2026-08-06T20:00:37Z (сверено отдельным пересчётом, не выводом кода)
    assert arbeitnow._normalize(_arb()).vacancy.created_at == "2026-08-06T20:00:37+00:00"


def test_arbeitnow_empty_location_is_remote_label():
    assert arbeitnow._normalize(_arb(location="")).vacancy.city == "Remote"


# ── himalayas ──────────────────────────────────────────────────────────────────

def test_himalayas_employer_comes_from_slug_not_placeholder():
    # companyName == "name" во ВСЕХ записях портала; настоящее имя только в слаге
    assert himalayas._normalize(_him()).vacancy.employer == "Bright Vision Technologies"


def test_himalayas_id_is_guid_tail():
    assert himalayas._normalize(_him()).vacancy.id == "himalayas_data-engineer-4189696294"


@pytest.mark.parametrize("period, minimum, maximum, expected_from, expected_to", [
    ("annual", 120_000, 180_000, 10_000, 15_000),     # /12
    ("monthly", 9_000, 12_000, 9_000, 12_000),        # как есть
    ("hourly", 80, 120, 12_800, 19_200),              # ×160 ч/мес
])
def test_himalayas_salary_is_normalised_to_monthly(period, minimum, maximum,
                                                   expected_from, expected_to):
    s = himalayas._normalize(_him(salaryPeriod=period, minSalary=minimum,
                                  maxSalary=maximum)).vacancy.salary
    assert (s.frm, s.to, s.currency) == (expected_from, expected_to, "USD")


def test_himalayas_unknown_salary_period_drops_the_range():
    # неизвестный период -> вилку не берём: ошибка в 12 раз хуже пустого поля
    assert himalayas._normalize(_him(salaryPeriod="weekly")).vacancy.salary is None


def test_himalayas_missing_range_is_none():
    assert himalayas._normalize(_him(minSalary=None, maxSalary=None)).vacancy.salary is None


def test_himalayas_foreign_salary_is_not_taxed_as_russian():
    # вычитать НДФЛ 13 % из зарубежной вилки нельзя — там своя налоговая система
    assert himalayas._normalize(_him()).vacancy.salary.gross is False


@pytest.mark.parametrize("seniority, expected", [
    (["Entry-level"], Experience.NONE),
    (["Mid-level"], Experience.BETWEEN_1_3),
    (["Senior"], Experience.BETWEEN_3_6),
    (["Director"], Experience.MORE_6),
    (["Entry-level", "Mid-level"], Experience.NONE),   # вилка грейдов -> самый младший
    ([], None),
])
def test_himalayas_seniority_maps_to_experience(seniority, expected):
    assert himalayas._normalize(_him(seniority=seniority)).vacancy.experience is expected


def test_himalayas_location_restrictions_become_city():
    v = himalayas._normalize(_him(locationRestrictions=["United States", "Canada"])).vacancy
    assert v.city == "United States, Canada"


def test_himalayas_no_restrictions_uses_the_shared_remote_label():
    """Пустой список = нанимают откуда угодно. Подпись — доменная REMOTE_CITY, одна на все
    источники: своя строка «Worldwide» давала ВТОРОЙ бакет в фасете городов ленты рядом с
    «Remote» от семи остальных порталов (замер 07.08.2026: 11 978 против 219)."""
    from hrwork.domain.models import REMOTE_CITY
    assert himalayas._normalize(_him(locationRestrictions=[])).vacancy.city == REMOTE_CITY
    assert REMOTE_CITY == "Remote"


def test_himalayas_is_always_remote():
    assert himalayas._normalize(_him()).vacancy.schedule is Schedule.REMOTE
