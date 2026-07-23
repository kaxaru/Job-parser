"""Тесты чистого домена: разбор сырой вакансии без какой-либо БД."""
import pytest

from etl.domain import Vacancy

pytestmark = pytest.mark.unit

FULL = {
    "id": "123",
    "_source": "hh",
    "name": "Python-разработчик (FastAPI)",
    "area": {"id": "1", "name": "Москва"},
    "salary": {"from": 150000, "to": 200000, "currency": "RUR", "gross": False},
    "experience": {"id": "between1And3"},
    "schedule": {"id": "fullDay"},
    "employer": {"name": "Acme"},
    "snippet": {"requirement": "Python, FastAPI, PostgreSQL, Docker", "responsibility": ""},
    "description_html": "<p>Возможна работа <b>удалённо</b>.</p>",
    "alternate_url": "https://hh.ru/vacancy/123",
    "_city": "Москва",
    "_query": "программист",
}


def test_salary_flattened():
    v = Vacancy.from_raw(FULL)
    assert (v.salary_min, v.salary_max) == (150000, 200000)
    assert v.salary_currency == "RUR"
    assert v.salary_gross is False


def test_salary_null():
    v = Vacancy.from_raw({**FULL, "salary": None})
    assert v.salary_min is None and v.salary_max is None
    assert v.salary_currency is None and v.salary_gross is None


def test_experience_and_schedule_mapped():
    v = Vacancy.from_raw(FULL)
    assert v.experience == "1–3 года"
    assert v.schedule == "Полный день"


def test_is_remote_from_text_marker():
    # schedule = fullDay, но в описании «удалённо» -> признак вычисляется
    assert Vacancy.from_raw(FULL).is_remote is True


def test_is_remote_from_schedule():
    rec = {**FULL, "schedule": {"id": "remote"}, "description_html": "офис"}
    assert Vacancy.from_raw(rec).is_remote is True


def test_not_remote():
    rec = {**FULL, "schedule": {"id": "fullDay"}, "snippet": {"requirement": "офис"},
           "description_html": "<p>работа в офисе</p>"}
    assert Vacancy.from_raw(rec).is_remote is False


def test_skills_extracted():
    skills = set(Vacancy.from_raw(FULL).skills)
    assert {"Python", "FastAPI", "PostgreSQL", "Docker"} <= skills
    assert "Java" not in skills


def test_skill_word_boundary():
    # 'good' не должно матчить Golang (\bgo\b)
    rec = {**FULL, "snippet": {"requirement": "good developer"},
           "description_html": "", "name": "dev"}
    assert "Golang" not in Vacancy.from_raw(rec).skills


def test_city_fallback_to_query_city():
    rec = {**FULL, "area": {}}
    assert Vacancy.from_raw(rec).city == "Москва"  # из _city


def test_id_is_string_namespaced():
    # id — строка: основной проект неймспейсит id по источникам, int() ронял 2/3 записей
    assert Vacancy.from_raw(FULL).id == "123"
    assert Vacancy.from_raw({**FULL, "id": "talanto_e9f687b5"}).id == "talanto_e9f687b5"
    assert Vacancy.from_raw({**FULL, "id": "hirify_733072"}).id == "hirify_733072"


def test_source_from_raw():
    assert Vacancy.from_raw({**FULL, "_source": "talanto"}).source == "talanto"
    assert Vacancy.from_raw({**FULL, "_source": "hirify"}).source == "hirify"


def test_source_defaults_to_hh_for_legacy():
    # legacy-запись без _source (старый срез) -> hh, а не пусто/краш
    rec = {k: val for k, val in FULL.items() if k != "_source"}
    assert Vacancy.from_raw(rec).source == "hh"


def test_city_capped_for_mssql_width():
    # города-агрегаторы (hirify/talanto) — списки регионов до ~2400 симв.; режем до CITY_MAX,
    # иначе MSSQL NVARCHAR(200)/UNIQUE-индекс падает (инцидент 2026-07-22)
    from etl.domain import CITY_MAX
    long_city = "Remote, " + ", ".join(["Country"] * 300)
    v = Vacancy.from_raw({**FULL, "area": {"name": long_city}})
    assert len(v.city) == CITY_MAX
    # обычный город не трогаем
    assert Vacancy.from_raw({**FULL, "area": {"name": "Москва"}}).city == "Москва"
