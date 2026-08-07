"""Адаптеры themuse.com и jobicy.com: маппинг внешней схемы в домен (ACL). Сеть не трогается.

Оба портала отдают ГРЕЙД полем — в отличие от arbeitnow и web3, где его нет вовсе и
`experience` остаётся пустым. Шкалы у всех свои, и их выравнивание — главное, что здесь
проверяется: один и тот же «senior» с любого портала обязан попасть в одну корзину, иначе
поедут и отбор кандидатов, и срезы аналитики.
"""
import datetime

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.sources import jobicy, themuse


def _muse(**kw):
    base = {
        "id": "17910578",
        "name": "Backend Engineer, Python",
        "contents": "<p>We use Python, FastAPI and PostgreSQL.</p>",
        "publication_date": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "short_name": "backend-engineer-89d09d",
        "locations": [{"name": "New York, NY"}],
        "categories": [{"name": "Software Engineering"}],
        "levels": [{"name": "Mid Level", "short_name": "mid"}],
        "tags": [],
        "refs": {"landing_page": "https://www.themuse.com/jobs/acme/backend-engineer-89d09d"},
        "company": {"id": 1, "short_name": "acme", "name": "Acme Inc"},
    }
    return {**base, **kw}


def _jobicy(**kw):
    base = {
        "id": "144843",
        "url": "https://jobicy.com/jobs/144843-python-engineer",
        "jobSlug": "144843-python-engineer",
        "jobTitle": "Python Engineer",
        "companyName": "Acme Inc",
        "jobIndustry": ["Software Engineering"],
        "jobType": ["Full-Time"],
        "jobGeo": "Anywhere",
        "jobLevel": "Midweight",
        "jobExcerpt": "Build backend services",
        "jobDescription": "<p>Python, FastAPI, PostgreSQL</p>",
        "pubDate": "2026-08-06T16:30:02+00:00",
    }
    return {**base, **kw}


# ── themuse ────────────────────────────────────────────────────────────────────

def test_themuse_maps_card_to_domain():
    r = themuse._normalize(_muse())
    v = r.vacancy
    assert v.id == "themuse_17910578"
    assert v.name == "Backend Engineer, Python"
    assert v.employer == "Acme Inc"
    assert v.city == "New York, NY"
    assert v.source == "themuse"
    assert r.url == "https://www.themuse.com/jobs/acme/backend-engineer-89d09d"


@pytest.mark.parametrize("levels, expected", [
    ([{"short_name": "internship"}], Experience.NONE),
    ([{"short_name": "entry"}], Experience.NONE),
    ([{"short_name": "mid"}], Experience.BETWEEN_1_3),
    ([{"short_name": "senior"}], Experience.BETWEEN_3_6),
    ([{"short_name": "management"}], Experience.MORE_6),
    ([{"short_name": "mid"}, {"short_name": "senior"}], Experience.BETWEEN_1_3),  # младший
    ([], None),
])
def test_themuse_levels_map_to_experience(levels, expected):
    assert themuse._normalize(_muse(levels=levels)).vacancy.experience is expected


@pytest.mark.parametrize("locations, expected", [
    ([{"name": "Flexible / Remote"}], Schedule.REMOTE),
    ([{"name": "Remote"}], Schedule.REMOTE),
    ([{"name": "New York, NY"}], Schedule.OFFICE),
])
def test_themuse_remote_is_read_from_location(locations, expected):
    # отдельного флага удалёнки у портала нет — она выражена локацией
    assert themuse._normalize(_muse(locations=locations)).vacancy.schedule is expected


def test_themuse_no_locations_is_labelled_remote():
    assert themuse._normalize(_muse(locations=[])).vacancy.city == "Remote"


def test_themuse_has_no_salary():
    assert themuse._normalize(_muse()).vacancy.salary is None


# ── jobicy ─────────────────────────────────────────────────────────────────────

def test_jobicy_maps_card_to_domain():
    r = jobicy._normalize(_jobicy())
    v = r.vacancy
    assert v.id == "jobicy_144843"
    assert v.name == "Python Engineer"
    assert v.employer == "Acme Inc"
    assert v.source == "jobicy"
    assert v.schedule is Schedule.REMOTE          # портал целиком про удалёнку
    assert r.url == "https://jobicy.com/jobs/144843-python-engineer"


@pytest.mark.parametrize("level, expected", [
    ("Entry-Level, Junior", Experience.NONE),     # вилка грейдов -> самый младший
    ("Junior", Experience.BETWEEN_1_3),
    ("Midweight", Experience.BETWEEN_1_3),
    ("Senior", Experience.BETWEEN_3_6),
    ("Director", Experience.MORE_6),
    ("Any", None),                                # «грейд не важен» — не «без опыта»
    ("", None),
])
def test_jobicy_level_maps_to_experience(level, expected):
    assert jobicy._normalize(_jobicy(jobLevel=level)).vacancy.experience is expected


@pytest.mark.parametrize("geo, expected", [
    ("Anywhere", "Anywhere"),
    ("USA", "USA"),
    ("APAC,  Australia", "APAC, Australia"),      # лишние пробелы схлопываются
    ("", "Remote"),
])
def test_jobicy_geo_becomes_city(geo, expected):
    # география найма — то же, что locationRestrictions у himalayas: «remote» на глобальном
    # рынке чаще всего значит «remote в пределах одной страны»
    assert jobicy._normalize(_jobicy(jobGeo=geo)).vacancy.city == expected


def test_jobicy_iso_date_is_kept():
    assert jobicy._normalize(_jobicy()).vacancy.created_at == "2026-08-06T16:30:02+00:00"


def test_jobicy_bad_date_does_not_crash():
    assert jobicy._normalize(_jobicy(pubDate="не дата")).vacancy.created_at is None
