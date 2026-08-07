"""Структурные тесты сборки дашборда (build_dashboard) на in-memory вакансиях.

build_dashboard читает репозиторий и пишет в data/ — оба перенаправлены в tmp, FX-курсы
и репозиторий подменены, поэтому тест гермётичен (без сети/БД и без записи в реальный data/).
Подменять надо ОБА держателя репозитория — `dashboard` и `funnel`: пропущенный второй читал
настоящий кеш вакансий, и модуль отъедал 123 с из 125 с всего бэкенд-прогона (08.08.2026).
Проверяем собранную СТРУКТУРУ HTML: присутствие графиков-панелей, итоги (вакансий/городов),
вкладки источников для мультипортального входа и деградацию на пустом входе.
"""
import datetime

import pytest

from hrwork.application import funnel
from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net import rates
from hrwork.infrastructure.storage import VacancyRecord
from hrwork.presentation.views import dashboard

# FX-курсы (per-USD) фиксируем -> без сети. USD 1:1, RUB 100 -> hirify(USD) нормализуется в RUB.
_FX = {"USD": 1.0, "RUB": 100.0, "EUR": 0.9}


class _Repo:
    """Заглушка vacancy_repository: отдаёт готовые записи вместо чтения vacancies_raw.json."""

    def __init__(self, records):
        self._records = records

    def load(self):
        return self._records


def _days_ago(n: int) -> str:
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    return (now - datetime.timedelta(days=n)).isoformat()


def _rec(vid, city, mid, *, currency="RUR", source="hh", schedule="fullDay",
         techs=(), employer="", age=10, role=Role.DEVELOPER):
    v = Vacancy(
        id=vid, name="x", city=city, city_id="1",
        salary=Salary(mid, None, currency) if mid is not None else None,
        experience=Experience.from_code("between1And3"),
        schedule=Schedule.from_code(schedule) or Schedule.OFFICE,
        techs=list(techs), role=role, employer=employer,
        created_at=_days_ago(age), source=source,
    )
    return VacancyRecord(vacancy=v)


def _multi_source_records():
    # Москва (hh) x3 + СПб (hirify) x2 -> 5 вакансий, 2 города, 2 портала.
    return [
        _rec("m1", "Москва", 200_000, source="hh", schedule="remote",
             techs=["Python", "Docker"], employer="Acme", age=5),
        _rec("m2", "Москва", 250_000, source="hh", schedule="fullDay",
             techs=["Python"], employer="Acme", age=90),          # ghost
        _rec("m3", "Москва", 180_000, source="hh", schedule="flexible",
             techs=["JavaScript"], employer="Beta", age=40),
        _rec("s1", "Санкт-Петербург", 3_000, currency="USD", source="hirify",
             schedule="remote", techs=["Python"], employer="Gamma", age=10),
        _rec("s2", "Санкт-Петербург", 4_000, currency="USD", source="hirify",
             schedule="fullDay", techs=["TypeScript"], employer="Gamma", age=70),
    ]


def _build(mp, out_dir, records):
    """Перенаправить пути dashboard в tmp, подменить репозиторий/FX и собрать -> HTML-текст."""
    data = out_dir / "data"
    mp.setattr(rates, "get_rates", lambda: dict(_FX))
    mp.setattr(dashboard, "REPORTS_DIR", out_dir / "reports")
    mp.setattr(dashboard, "DATA_DIR", data)
    mp.setattr(dashboard, "DASHBOARD_OUT", data / "dashboard.html")
    mp.setattr(dashboard, "vacancy_repository", lambda: _Repo(records))
    # Воронку тоже изолируем: `funnel` держит СВОЮ ссылку на vacancy_repository, и подмена
    # только в `dashboard` её не трогала — сборка читала настоящий data/vacancies_raw.json
    # (104 890 вакансий, 38 из 39 с прогона) плюс живые статусы и чаты. Тест был не
    # гермётичен вопреки докстрингу и зависел от локальных данных запускающего (08.08.2026).
    mp.setattr(funnel, "vacancy_repository", lambda: _Repo(records))
    mp.setattr(funnel.store, "statuses", dict)
    mp.setattr(funnel.store, "chat_messages", dict)
    mp.setattr(funnel.store, "applied_log", list)
    dashboard.build_dashboard()
    return (data / "dashboard.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def multi_source_html(tmp_path_factory):
    """Собрать мультипортальный дашборд один раз (build_dashboard дорогой: ~десятки фигур)."""
    out = tmp_path_factory.mktemp("dash")
    with pytest.MonkeyPatch.context() as mp:
        return _build(mp, out, _multi_source_records())


@pytest.mark.parametrize("pane_id", [
    "pane-all-cities",        # всегда: 01_cities.csv генерится безусловно
    "pane-all-hm_tech",       # тепловая карта технологий
    "pane-all-remote",        # удалёнка по городам
    "pane-all-companies",     # топ работодателей (есть employer)
    "pane-all-by_source",     # разрез по порталам — только при >1 источнике
    "pane-hh-cities",         # по-портальный срез hh
    "pane-hirify-cities",     # по-портальный срез hirify
])
def test_dashboard_contains_chart_pane(pane_id, multi_source_html):
    assert f'id="{pane_id}"' in multi_source_html


@pytest.mark.parametrize("src", ["all", "hh", "hirify"])
def test_dashboard_shows_source_tab_per_portal(src, multi_source_html):
    # Мультипортальный вход -> навигация источников с кнопкой на каждый портал.
    assert f'id="src-{src}"' in multi_source_html


@pytest.mark.parametrize("needle", ["5 вакансий", "2 городов"])
def test_dashboard_reports_dynamic_totals(needle, multi_source_html):
    # Итоги считаются из 01_cities.csv, без хардкода: 5 вакансий в 2 городах.
    assert needle in multi_source_html


@pytest.fixture(scope="module")
def empty_html(tmp_path_factory):
    """Дашборд на пустом входе — тоже ОДИН раз на модуль: тесты ниже смотрят один HTML,
    а раньше каждый строил свой."""
    out = tmp_path_factory.mktemp("dash_empty")
    with pytest.MonkeyPatch.context() as mp:
        return _build(mp, out, [])


def test_empty_input_dashboard_degrades_gracefully(empty_html):
    # Пустой вход не роняет сборку: HTML пишется, итоги — нули, базовый график присутствует.
    assert "0 вакансий" in empty_html
    assert 'id="pane-all-cities"' in empty_html    # cities-график рендерится даже на пустом CSV


def test_empty_input_dashboard_has_no_source_tabs(empty_html):
    # Один (нулевой) источник -> навигация источников не рисуется.
    assert 'class="src-tabs"' not in empty_html
