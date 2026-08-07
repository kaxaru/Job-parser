"""Структурные тесты сборки ленты (build_feed) -> payload feed-data.js.

Отдельно от test_sanitize.py (тот про XSS-санитайзер): здесь проверяем, что сборщик
эмитит ожидаемые JS-глобалы и что карточные поля вакансии переживают round-trip
Python -> JSON -> feed-data.js. Всё in-memory: репозиторий, store, FX-курсы и esbuild
подменены, поэтому ни сети, ни браузера, ни записи в реальный data/.
"""
import json

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net import rates
from hrwork.infrastructure.storage import VacancyRecord
from hrwork.presentation.views import feed

# FX-курсы (per-USD) фиксируем -> тесты гермётичны, без сети. USD 1:1, RUB 100.
_FX = {"USD": 1.0, "RUB": 100.0, "EUR": 0.9}
_MARKS = {"v1": "applied"}
_STATUSES = {"v1": "INVITATION"}     # у v1 есть статус работодателя из чата
_FORMS = {"v2": {"name": "Аналитик данных"}}   # v2 — вакансия-опросник (нужна форма)
# v1 — форма протухла при свипе (status=empty) -> карточка гасится как «не актуальна»
_FORM_CACHE = {"v1": {"status": "empty"}, "v2": {"status": "ok"}}


class _Repo:
    """Заглушка vacancy_repository: отдаёт готовые записи вместо чтения файла."""

    def __init__(self, records):
        self._records = records

    def load(self):
        return self._records


def _record(vid, *, name, city, employer, source, mid, currency, exp, schedule,
            techs, role, url, desc="", req=""):
    v = Vacancy(
        id=vid, name=name, city=city, city_id="1",
        salary=Salary(mid, None, currency) if mid is not None else None,
        experience=Experience.from_code(exp),
        schedule=Schedule.from_code(schedule) or Schedule.OFFICE,
        techs=list(techs), role=role, employer=employer,
        created_at="2026-06-01T00:00:00+00:00", responses=7 if vid == "v1" else None,
        source=source,
    )
    return VacancyRecord(vacancy=v, url=url, description_html=desc, requirement=req)


def _records():
    return [
        # remote -> remote_any=True; есть статус, нет формы; резюме-валюта RUR
        _record("v1", name="Python Backend", city="Москва", employer="Acme",
                source="hh", mid=200_000, currency="RUR", exp="between1And3",
                schedule="remote", techs=["Python", "Docker"], role=Role.BACKEND,
                url="https://hh.ru/vacancy/v1", desc="<p>Описание</p>", req="Требования"),
        # office + текст без маркеров -> remote_any=False; нет статуса, нужна форма; USD
        _record("v2", name="Аналитик данных", city="Санкт-Петербург", employer="Gamma",
                source="hirify", mid=3_000, currency="USD", exp="between3And6",
                schedule="fullDay", techs=["SQL"], role=Role.ANALYST,
                url="https://hirify.io/v2", desc="", req="Офисная работа"),
    ]


def _const(js_text: str, name: str):
    """Достать значение JS-константы `const NAME = <json>;` из feed-data.js -> Python-объект.
    json.dumps(ensure_ascii=False) не эмитит переносов -> каждая константа на своей строке."""
    prefix = f"const {name} = "
    for line in js_text.splitlines():
        if line.startswith(prefix):
            payload = line[len(prefix):]
            assert payload.endswith(";"), f"строка {name} без завершающей ;"
            return json.loads(payload[:-1])
    raise KeyError(name)


def _build_feed_data(out_dir, **config_overrides):
    """Собрать ленту в `out_dir` с подменёнными зависимостями и вернуть текст feed-data.js.

    Свой MonkeyPatch-контекст: фикстура `monkeypatch` функциональная и в модульную не годится.
    `config_overrides` — атрибуты модуля `feed` (константы конфига), чтобы проверять инжекцию
    настроек профиля, не трогая файл на диске."""
    data = out_dir / "data"
    records = _records()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(feed, "vacancy_repository", lambda: _Repo(records))
        mp.setattr(feed.store, "statuses", lambda: dict(_STATUSES))
        mp.setattr(feed.store, "forms", lambda: dict(_FORMS))
        mp.setattr(feed.store, "form_cache", lambda: dict(_FORM_CACHE))
        mp.setattr(feed.store, "marks", lambda: dict(_MARKS))
        mp.setattr(feed.rates, "get_rates", lambda: dict(_FX))
        mp.setattr(feed.shutil, "which", lambda _name: None)   # esbuild off -> не собираем feed.js
        mp.setattr(feed, "DATA_DIR", data)
        mp.setattr(feed, "FEED_OUT", data / "feed.html")
        for name, value in config_overrides.items():
            mp.setattr(feed, name, value)
        feed.build_feed()
        return (data / "feed-data.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def feed_globals(tmp_path_factory):
    """Собрать ленту один раз (build_feed дорогой) и вернуть распарсенный feed-data.js."""
    js_text = _build_feed_data(tmp_path_factory.mktemp("feed"))

    vac_list = _const(js_text, "VACANCIES")
    return {
        "text": js_text,
        "vacancies_by_id": {v["id"]: v for v in vac_list},
        "sal_max": _const(js_text, "SAL_MAX"),
        "saved_marks": _const(js_text, "SAVED_MARKS"),
        "fx_rates": _const(js_text, "FX_RATES"),
        "fx_alias": _const(js_text, "FX_ALIAS"),
    }


@pytest.mark.parametrize("name", [
    "VACANCIES", "SAL_MAX", "SAVED_MARKS", "FX_RATES", "FX_ALIAS",
    "STATE_LABELS_PY", "RESUME_CORE_PY", "RESUME_EXPS_PY", "MARK_VALUES_PY",
    "CHAT_FROZEN_PY",
    # PORTAL_SITES_PY добавлен 01.08.2026 и в этот список НЕ попал — страж отставал от
    # кода почти неделю (найдено аудитом 07.08). Он и есть мост подписей порталов в JS,
    # без которого talanto и getmatch подписывались как «hh.ru».
    "PORTAL_SITES_PY",
    # наборы состояний отклика — раньше JS решал сам через startsWith('DISCARD'),
    # и это расходилось с Python по DISCARD_BY_APPLICANT (аудит 07.08.2026)
    "DISCARD_STATES_PY", "INVITED_STATES_PY",
    # шаблоны писем ленты из resume_profile.json (08.08.2026): форк должен править письма
    # профилем, а не src/feed/cover.js. Пустой список -> cover.js берёт свои дефолты.
    "FEED_COVER_TEMPLATES_PY",
])
def test_feed_data_defines_expected_global(name, feed_globals):
    # Каждый глобал, от которого зависит JS-лента, обязан присутствовать в бандле.
    assert f"const {name} = " in feed_globals["text"]


def test_globals_guard_covers_every_injected_const(feed_globals):
    """Список выше не должен отставать от `feed.py::build_feed`.

    Именно это и произошло с `PORTAL_SITES_PY`: глобал появился в коде, а страж его не знал,
    и «каждый глобал обязан присутствовать» проверялось для десяти из одиннадцати. Считаем
    фактические `const` в бандле и сверяем с числом случаев параметризации."""
    import re
    actual = set(re.findall(r"^const ([A-Z_]+) = ", feed_globals["text"], re.M))
    guarded = set(
        test_feed_data_defines_expected_global.pytestmark[0].args[1]  # type: ignore[attr-defined]
    )
    assert actual == guarded, f"не под стражем: {sorted(actual - guarded)}"


def test_feed_cover_templates_injected_from_profile(tmp_path):
    """Письма ленты правятся `resume_profile.json`, а не `src/feed/cover.js`: форк с другим
    резюме не должен трогать исходник (08.08.2026). В JSON нельзя положить функцию, поэтому
    едут СТРОКИ с подстановками, а оборачивает их `cover.js::coverTemplates()`."""
    js = _build_feed_data(tmp_path, FEED_COVER_TEMPLATES=["Здравствуйте! Вакансия {role}{company}."])
    assert _const(js, "FEED_COVER_TEMPLATES_PY") == ["Здравствуйте! Вакансия {role}{company}."]


def test_feed_cover_templates_empty_without_profile(tmp_path):
    # ключа в профиле нет -> пустой список -> cover.js берёт свои дефолты, поведение прежнее.
    # Значение задаём явно: иначе тест читал бы профиль запускающего и падал бы у того,
    # кто свои шаблоны как раз настроил.
    js = _build_feed_data(tmp_path, FEED_COVER_TEMPLATES=[])
    assert _const(js, "FEED_COVER_TEMPLATES_PY") == []


def test_frozen_chat_kinds_injected_from_python(feed_globals):
    # тупиковые виды чата — единый источник в chat_class.FROZEN_KINDS; хардкод 'ack' в JS
    # разъезжался бы с Python (в ленте он был в трёх местах). Ожидаемое — литерал, а не
    # list(FROZEN_CODES): иначе тест повторил бы реализацию и прошёл при любой её ошибке.
    assert _const(feed_globals["text"], "CHAT_FROZEN_PY") == ["ack", "bot_interview"]


@pytest.mark.parametrize("vid, field, expected", [
    ("v1", "name", "Python Backend"),
    ("v1", "url", "https://hh.ru/vacancy/v1"),
    ("v1", "employer", "Acme"),
    ("v1", "city", "Москва"),
    ("v1", "sal_from", 200_000),
    ("v1", "sal_mid", 200_000),
    ("v1", "currency", "RUR"),
    ("v1", "exp", "1–3 года"),          # Experience.label (EXP_LABELS['between1And3'])
    ("v1", "schedule", "remote"),
    ("v1", "techs", ["Python", "Docker"]),
    ("v1", "remote_any", True),         # schedule=remote
    ("v1", "role", "Backend"),
    ("v1", "resp", 7),
    ("v1", "status", "INVITATION"),     # из store.statuses
    ("v1", "needs_form", False),
    ("v1", "form_dead", True),          # свип: status=empty -> не актуальна
    ("v1", "source", "hh"),
    ("v2", "sal_mid", 3_000),
    ("v2", "currency", "USD"),
    ("v2", "exp", "3–6 лет"),
    ("v2", "schedule", "fullDay"),
    ("v2", "remote_any", False),        # office + текст без маркеров удалёнки
    ("v2", "role", "Аналитик"),
    ("v2", "resp", None),
    ("v2", "status", None),             # нет чата -> статуса нет
    ("v2", "needs_form", True),         # v2 в store.forms
    ("v2", "form_dead", False),         # кеш ok -> живая
    ("v2", "source", "hirify"),
])
def test_vacancy_card_field_survives_roundtrip(vid, field, expected, feed_globals):
    assert feed_globals["vacancies_by_id"][vid][field] == expected


def test_mark_values_bridge_matches_python(feed_globals):
    # словарь пометок в JS = инжект из marks.py (иначе разъезжается: инцидент "discard")
    # литерал, а не list(MARK_VALUES): иначе тест повторяет реализацию и пройдёт при её ошибке
    assert _const(feed_globals["text"], "MARK_VALUES_PY") == ["applied", "rejected"]


@pytest.mark.parametrize("field", ["id", "age", "gap", "fresh"])
def test_freshness_and_id_fields_present_in_card(field, feed_globals):
    # Свежесть (age/gap/fresh) и id — структурные поля карточки, лента их читает всегда.
    assert field in feed_globals["vacancies_by_id"]["v1"]


def test_sal_max_is_largest_card_mid(feed_globals):
    # SAL_MAX = верх ползунка зарплаты -> максимальная серединка среди карточек.
    assert feed_globals["sal_max"] == 200_000


def test_saved_marks_pass_through_unchanged(feed_globals):
    assert feed_globals["saved_marks"] == _MARKS


def test_fx_rates_and_alias_match_injected_sources(feed_globals):
    # FX_RATES — ровно инжектированные курсы; FX_ALIAS — единый источник rates.CURRENCY_ALIAS.
    assert feed_globals["fx_rates"] == _FX
    assert feed_globals["fx_alias"] == rates.CURRENCY_ALIAS
