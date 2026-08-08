"""Тесты чистого домена: разбор сырой вакансии без какой-либо БД."""
import ast
import hashlib
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from etl import domain, rates
from etl.domain import (
    NO_CITY_LABEL,
    NO_EXPERIENCE_LABEL,
    PARENT_DETECT_SIG,
    REMOTE_LIKE_CODES,
    REMOTE_MARKERS,
    SCHEDULE,
    Vacancy,
)

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

# Синтетические курсы per-USD: круглые, чтобы ожидаемые рубли писались литералом.
FX = {"USD": 1.0, "RUB": 100.0, "EUR": 0.5}


@pytest.fixture(autouse=True)
def _fx_offline(monkeypatch):
    """Домен по умолчанию НЕ ходит на диск за курсами: юнит не должен зависеть от того,
    какой сегодня курс в data/fx_rates.json. Где рубли важны, курсы передаются явно (fx=FX)."""
    rates.reset_cache()
    monkeypatch.setattr(domain, "load_rates", dict)


def test_salary_flattened():
    v = Vacancy.from_raw(FULL, fx=FX)
    assert (v.salary_min, v.salary_max) == (150000, 200000)
    assert v.salary_currency == "RUB"      # RUR канонизирован
    assert v.salary_gross is False


def test_salary_null():
    v = Vacancy.from_raw({**FULL, "salary": None}, fx=FX)
    assert v.salary_min is None and v.salary_max is None
    assert v.salary_min_rub is None and v.salary_max_rub is None
    assert v.salary_currency is None and v.salary_gross is None


# ── валюта и рублёвые суммы ──────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("RUR", "RUB"),
    ("RUB", "RUB"),
    ("rur", "RUB"),
    ("BYR", "BYN"),
    ("BYN", "BYN"),
    ("USDT", "USD"),
    ("eur", "EUR"),
    ("", None),
    (None, None),
])
def test_salary_currency_canonized(raw, expected):
    # 09.08.2026: RUR и RUB были РАЗНЫМИ бакетами витрин (15 726 против 1 998 записей),
    # BYR/BYN и USDT/USD — тоже; пустая валюта не значит «рубли».
    v = Vacancy.from_raw({**FULL, "salary": {"from": 1, "currency": raw}}, fx=FX)
    assert v.salary_currency == expected


@pytest.mark.parametrize(("currency", "expected_min", "expected_max"), [
    ("RUB", 150000.0, 200000.0),
    ("RUR", 150000.0, 200000.0),
    ("USD", 15000000.0, 20000000.0),
    ("EUR", 30000000.0, 40000000.0),
    ("UZS", None, None),        # курса нет -> вакансия выпадает из зарплатного среза
    ("", None, None),           # валюты нет -> не «рубли по умолчанию»
])
def test_salary_converted_to_rub(currency, expected_min, expected_max):
    rec = {**FULL, "salary": {"from": 150000, "to": 200000, "currency": currency}}
    v = Vacancy.from_raw(rec, fx=FX)
    assert (v.salary_min_rub, v.salary_max_rub) == (expected_min, expected_max)


def test_salary_native_amounts_kept_alongside_rub():
    # нативная вилка нужна карточке, рублёвая — агрегатам; одно не подменяет другое
    rec = {**FULL, "salary": {"from": 1000, "to": None, "currency": "USD"}}
    v = Vacancy.from_raw(rec, fx=FX)
    assert (v.salary_min, v.salary_max) == (1000, None)
    assert (v.salary_min_rub, v.salary_max_rub) == (100000.0, None)


@pytest.mark.parametrize(("amount", "currency", "expected"), [
    (1000, "USD", 100000.0),
    (1000, "usdt", 100000.0),
    (1000, "EUR", 200000.0),
    (1000, "RUR", 1000.0),
    (1000, "KRW", None),
    (1000, "", None),
    (1000, None, None),
    (None, "USD", None),
])
def test_to_rub(amount, currency, expected):
    assert rates.to_rub(amount, currency, FX) == expected


@pytest.mark.real_fx
def test_load_rates_reads_parent_cache(tmp_path):
    path = tmp_path / "fx_rates.json"
    path.write_text('{"fetched_at": 1, "rates": {"USD": 1.0, "RUB": 80.0}}', encoding="utf-8")
    assert rates.load_rates(path) == {"USD": 1.0, "RUB": 80.0}


@pytest.mark.real_fx
def test_load_rates_without_file_degrades_to_empty(tmp_path):
    # нет кеша -> пусто и предупреждение, а не падение прогона и не поход в сеть
    assert rates.load_rates(tmp_path / "fx_rates.json") == {}


# ── справочники: пустой код внешнего источника ───────────────────────────────

@pytest.mark.parametrize(("code", "expected"), [
    ("noExperience", "Без опыта"),
    ("between1And3", "1–3 года"),
    ("between3And6", "3–6 лет"),
    ("moreThan6", "6+ лет"),
    ("someNewCode", "someNewCode"),   # неизвестный НЕпустой код проходит как есть
    ("", None),
    (None, None),
])
def test_experience_mapped(code, expected):
    # 09.08.2026: пустой код опыта уходил в витрину пустой подписью, и защита
    # `coalesce(experience, 'не указан')` не срабатывала никогда
    assert Vacancy.from_raw({**FULL, "experience": {"id": code}}, fx=FX).experience == expected


@pytest.mark.parametrize(("code", "expected"), [
    ("fullDay", "Офис"),
    ("remote", "Удалённо"),
    ("flexible", "Гибрид"),
    ("someNewCode", "someNewCode"),
    ("", None),
    (None, None),
])
def test_schedule_mapped(code, expected):
    assert Vacancy.from_raw({**FULL, "schedule": {"id": code}}, fx=FX).schedule == expected


@pytest.mark.parametrize(("field", "patch"), [
    ("employer", {"employer": {"name": ""}}),
    ("employer", {"employer": {}}),
    ("employer", {"employer": None}),
    ("city", {"area": {"name": ""}, "_city": ""}),
    ("url", {"alternate_url": ""}),
])
def test_empty_string_from_source_is_absence(field, patch):
    # 09.08.2026: пустое имя работодателя доезжало до измерения строкой и собирало вокруг
    # себя каждую седьмую вакансию — «Топ-15 работодателей» возглавлял безымянный
    assert getattr(Vacancy.from_raw({**FULL, **patch}, fx=FX), field) is None


# ── удалёнка ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("code", "expected"), [
    ("remote", True),
    ("flexible", True),      # гибрид — тоже удалёнкоподобность
    ("fullDay", False),
    ("someNewCode", False),
    ("", False),
    (None, False),
])
def test_is_remote_covers_remote_and_hybrid_codes(code, expected):
    # 09.08.2026: стенд считал удалёнкой «код remote ИЛИ маркер в тексте» — третье
    # определение в проекте; гибрид попадал в офис, офис с «удалённо» в описании — в удалёнку
    rec = {**FULL, "schedule": {"id": code}, "snippet": {}, "description_html": "офис"}
    assert Vacancy.from_raw(rec, fx=FX).is_remote is expected


@pytest.mark.parametrize(("description", "expected"), [
    ("<p>Возможна работа <b>удалённо</b>.</p>", True),
    ("<p>Fully remote team</p>", True),
    ("<p>Можно работать из любой точки мира</p>", True),
    ("<p>Работа в офисе</p>", False),
    ("", False),
])
def test_remote_mentioned_reports_text_marker(description, expected):
    rec = {**FULL, "snippet": {}, "description_html": description}
    assert Vacancy.from_raw(rec, fx=FX).remote_mentioned is expected


def test_text_marker_does_not_make_office_vacancy_remote():
    # два РАЗНЫХ понятия: формат работы (аналитический срез) и упоминание удалёнки в тексте
    # (отбор под отклик, у родителя — feed.py::remote_any)
    rec = {**FULL, "schedule": {"id": "fullDay"},
           "description_html": "<p>Иногда можно работать удалённо</p>"}
    v = Vacancy.from_raw(rec, fx=FX)
    assert (v.is_remote, v.remote_mentioned) == (False, True)


def test_hybrid_is_remote_without_any_text_marker():
    rec = {**FULL, "schedule": {"id": "flexible"}, "snippet": {},
           "description_html": "<p>Два дня в офисе</p>"}
    v = Vacancy.from_raw(rec, fx=FX)
    assert (v.is_remote, v.remote_mentioned) == (True, False)


# ── стек ─────────────────────────────────────────────────────────────────────

def test_skills_extracted():
    assert Vacancy.from_raw(FULL, fx=FX).skills == ("Python", "FastAPI", "PostgreSQL", "Docker")


@pytest.mark.parametrize(("title", "expected"), [
    ("Курьер Яндекс Go", ()),
    ("go to the office every day", ()),
    ("good developer", ()),
    ("Go-разработчик", ("Go",)),
    ("Backend developer (Golang)", ("Go",)),
    ("Программист Go", ("Go",)),
])
def test_go_tag_requires_dev_context(title, expected):
    # 09.08.2026: паттерн `\bgo\b(?=\W)` вешал тег на курьеров «Яндекс Go» — три четверти
    # всех «Golang» в витрине были ложными
    rec = {**FULL, "name": title, "snippet": {}, "description_html": ""}
    assert Vacancy.from_raw(rec, fx=FX).skills == expected


def test_skills_taken_from_parent_cache_when_signature_matches():
    # родитель кладёт готовый стек в `_techs` вместе с сигнатурой словаря `_dv` — ровно
    # чтобы потребители не считали его своим словарём и витрина сходилась с лентой
    rec = {**FULL, "name": "Data Engineer", "description_html": "",
           "snippet": {"requirement": "Airflow, dbt, Python"},
           "_techs": ["ML/AI", "AWS", "1С"], "_dv": PARENT_DETECT_SIG}
    assert Vacancy.from_raw(rec, fx=FX).skills == ("ML/AI", "AWS", "1С", "Airflow", "dbt")


def test_skills_recomputed_when_dictionary_signature_is_stale():
    rec = {**FULL, "name": "Data Engineer", "description_html": "",
           "snippet": {"requirement": "Airflow, dbt, Python"},
           "_techs": ["ML/AI", "AWS", "1С"], "_dv": "000000000000"}
    assert Vacancy.from_raw(rec, fx=FX).skills == ("Python", "Airflow", "dbt")


def test_analytics_tags_added_on_top_of_parent_cache():
    # теги стенда (Airflow/dbt/Greenplum/…) родителю неизвестны и обязаны считаться всегда
    rec = {**FULL, "name": "BI-разработчик", "description_html": "",
           "snippet": {"requirement": "Superset, Metabase, Power BI"},
           "_techs": ["Python"], "_dv": PARENT_DETECT_SIG}
    assert Vacancy.from_raw(rec, fx=FX).skills == ("Python", "Superset", "Metabase", "Power BI")


# ── прочее ───────────────────────────────────────────────────────────────────

def test_location_without_a_name_is_absence():
    # 09.08.2026: фолбэк на служебное `_city` убран вместе с `_query` — родитель этих
    # полей больше не пишет ни в одной из 109 597 записей, и фолбэк на пустоту
    # притворялся бы данными. Пусто -> None, бакет витрины подпишет `NO_CITY_LABEL`.
    assert Vacancy.from_raw({**FULL, "area": {}}, fx=FX).city is None


def test_id_is_string_namespaced():
    # id — строка: основной проект неймспейсит id по источникам, int() ронял 2/3 записей
    assert Vacancy.from_raw(FULL, fx=FX).id == "123"
    assert Vacancy.from_raw({**FULL, "id": "talanto_e9f687b5"}, fx=FX).id == "talanto_e9f687b5"
    assert Vacancy.from_raw({**FULL, "id": "hirify_733072"}, fx=FX).id == "hirify_733072"


def test_source_from_raw():
    assert Vacancy.from_raw({**FULL, "_source": "talanto"}, fx=FX).source == "talanto"
    assert Vacancy.from_raw({**FULL, "_source": "hirify"}, fx=FX).source == "hirify"


def test_source_defaults_to_hh_for_legacy():
    # legacy-запись без _source (старый срез) -> hh, а не пусто/краш
    rec = {k: val for k, val in FULL.items() if k != "_source"}
    assert Vacancy.from_raw(rec, fx=FX).source == "hh"


def test_city_capped_for_mssql_width():
    # города-агрегаторы (hirify/talanto) — списки регионов до ~2400 симв.; режем до CITY_MAX,
    # иначе MSSQL NVARCHAR(200)/UNIQUE-индекс падает (инцидент 2026-07-22)
    from etl.domain import CITY_MAX
    long_city = "Remote, " + ", ".join(["Country"] * 300)
    v = Vacancy.from_raw({**FULL, "area": {"name": long_city}}, fx=FX)
    assert len(v.city) == CITY_MAX
    # обычный город не трогаем
    assert Vacancy.from_raw({**FULL, "area": {"name": "Москва"}}, fx=FX).city == "Москва"


# ── страж-тесты: копии констант родителя не должны разъехаться ───────────────
# Константы читаем ИЗ ИСХОДНИКОВ родителя AST-разбором, а не импортом: `import hrwork.config`
# на импорте делает mkdir data/ и logs/, load_dotenv, переконфигурирует loguru и читает
# resume_profile.json (сам родитель называет это компромиссом в шапке
# `hrwork/domain/parsing.py`). Юниту стенда нужен текст констант, а не побочные эффекты.
# Исключение — `hrwork/domain/schedule.py`: там чистый enum, его импортируем как есть.

HRWORK_ROOT = Path(__file__).resolve().parents[2]
HRWORK = HRWORK_ROOT / "hrwork"
needs_parent = pytest.mark.skipif(
    not (HRWORK / "config.py").exists(),
    reason="родительский пакет hrwork рядом не развёрнут",
)


def _parent_literal(module: str, name: str):
    """Значение литеральной константы модуля родителя, без исполнения модуля."""
    tree = ast.parse((HRWORK / module).read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{module}: константа {name} не найдена")


@lru_cache(maxsize=1)
def _parent_tech_patterns():
    return _parent_literal("config.py", "TECH_PATTERNS")


@needs_parent
def test_remote_like_codes_match_parent_domain():
    sys.path.insert(0, str(HRWORK_ROOT))
    from hrwork.domain.schedule import REMOTE_LIKE_CODES as parent_codes
    assert parent_codes == REMOTE_LIKE_CODES


@needs_parent
def test_remote_markers_match_parent_domain():
    assert _parent_literal("domain/parsing.py", "REMOTE_MARKERS") == REMOTE_MARKERS


@needs_parent
def test_currency_alias_matches_parent():
    assert _parent_literal(
        "infrastructure/net/rates.py", "CURRENCY_ALIAS") == rates.CURRENCY_ALIAS


@needs_parent
def test_parent_detect_sig_pin_is_current():
    """Пин сигнатуры словаря стека протух -> кеш `_techs` перестанет браться молча."""
    version = _parent_literal("domain/parsing.py", "_DETECT_ALGO_VERSION")
    digest = hashlib.blake2s(
        (repr(sorted(_parent_tech_patterns().items())) + f"|v{version}").encode("utf-8"),
        digest_size=6,
    ).hexdigest()
    assert digest == PARENT_DETECT_SIG


@needs_parent
@pytest.mark.parametrize("tag", sorted(domain.STACK_SKILL_PATTERNS))
def test_stack_patterns_are_verbatim_copies_of_parent(tag):
    assert domain.STACK_SKILL_PATTERNS[tag] == _parent_tech_patterns().get(tag)


@needs_parent
def test_etl_specific_tags_do_not_shadow_parent_tags():
    """Аналитические теги стенда — ДОПОЛНЕНИЕ к словарю родителя, а не вторая версия его тегов."""
    assert sorted(set(domain.ETL_SKILL_PATTERNS) & set(_parent_tech_patterns())) == []


# ─── Подписи справочников и бакет «не указан» (аудит 09.08.2026) ────────────────
_SCHEMAS = list((Path(__file__).resolve().parents[1] / "etl" / "sql").rglob("schema.sql"))


@pytest.mark.parametrize("code, label", [
    ("fullDay", "Офис"),
    ("remote", "Удалённо"),
    ("flexible", "Гибрид"),
])
def test_schedule_labels_match_the_parent_domain(code, label):
    """Подписи дословно как у родителя. «Гибкий график» вместо «Гибрид» не косметика:
    с 09.08.2026 `flexible` попадает в удалёнку, и старая подпись это скрывала."""
    from hrwork.domain.schedule import Schedule
    assert SCHEDULE[code] == label
    assert Schedule.from_code(code).label == label


@pytest.mark.parametrize("dead_code", ["shift", "flyInFlyOut"])
def test_schedule_has_no_labels_for_formats_the_parent_never_produces(dead_code):
    # В домене родителя три члена; подпись для несуществующего кода обещает срез,
    # которого не будет никогда.
    assert dead_code not in SCHEDULE


@pytest.mark.parametrize("schema", _SCHEMAS, ids=lambda p: p.parent.name)
def test_marts_bucket_unknown_experience_under_the_same_label(schema):
    """SQL импортировать константу не умеет, поэтому равенство стережёт тест.
    Разойдись эти две строки — фильтр «Опыт» в Metabase перестанет доставать бакет."""
    assert NO_EXPERIENCE_LABEL in schema.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "schema", [p for p in _SCHEMAS
               if re.search(r"(?is)create[^;]*city_stats", p.read_text(encoding="utf-8"))],
    ids=lambda p: p.parent.name)
def test_marts_bucket_unknown_location_under_the_same_label(schema):
    """Тот же приём, что и с опытом: SQL импортировать константу не умеет, поэтому
    равенство подписи стережёт тест. ClickHouse в набор не входит намеренно — city_stats
    там нет, и это расхождение зафиксировано комментарием в самих схемах."""
    assert NO_CITY_LABEL in schema.read_text(encoding="utf-8")
