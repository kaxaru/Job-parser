"""Структурные тесты сборки дашборда (build_dashboard) на in-memory вакансиях.

build_dashboard читает репозиторий и пишет в data/ — оба перенаправлены в tmp, FX-курсы
и репозиторий подменены, поэтому тест гермётичен (без сети/БД и без записи в реальный data/).
Подменять надо ОБА держателя репозитория — `dashboard` и `funnel`: пропущенный второй читал
настоящий кеш вакансий, и модуль отъедал 123 с из 125 с всего бэкенд-прогона (08.08.2026).
Третий держатель — `_ensure_plotly`: он качает 3,5 МБ с cdn.plot.ly с таймаутом 60 с, а путь
локальной копии брал НАСТОЯЩИЙ data/ мимо подмены DATA_DIR (аудит 08.08.2026, п.58). Заглушены
оба: и загрузка, и `urlopen` — сборка, дошедшая до сети, падает, а не идёт наружу молча.
Проверяем собранную СТРУКТУРУ HTML: присутствие графиков-панелей, итоги (вакансий/городов),
вкладки источников для мультипортального входа и деградацию на пустом входе.
"""
import datetime
from types import SimpleNamespace

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


class _LogSpy:
    """Заглушка loguru-логгера: складывает (уровень, шаблон, аргументы) в список.
    Сводка прогона — контракт наблюдаемости, а не отладочный вывод, поэтому её проверяем."""

    def __init__(self, sink):
        self._sink = sink

    def __getattr__(self, level):
        def record(tmpl, *args):
            self._sink.append((level, tmpl, args))
        return record


class _NetworkForbidden(BaseException):
    """Поход в сеть из теста. НЕ Exception намеренно: `_ensure_plotly` ловит Exception и
    молча уходит на CDN — обычный assert он проглотил бы вместе с сигналом."""


def _no_network(*_args, **_kwargs):
    raise _NetworkForbidden("сборка дашборда не должна ходить в сеть")


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


def _build(mp, out_dir, records, logs=None):
    """Перенаправить пути dashboard в tmp, подменить репозиторий/FX и собрать -> HTML-текст."""
    data = out_dir / "data"
    mp.setattr(rates, "get_rates", lambda: dict(_FX))
    mp.setattr(dashboard, "REPORTS_DIR", out_dir / "reports")
    mp.setattr(dashboard, "DATA_DIR", data)
    mp.setattr(dashboard, "DASHBOARD_OUT", data / "dashboard.html")
    mp.setattr(dashboard, "vacancy_repository", lambda: _Repo(records))
    # plotly.js: без заглушки сборка качает 3,5 МБ с CDN (60 с таймаута) и кладёт их
    # в личный data/ запускающего. Второй гард — сам urlopen: он ловит ЛЮБОЙ поход
    # в сеть из сборки, а не только этот (аудит 08.08.2026, п.58).
    mp.setattr(dashboard, "_ensure_plotly", lambda: None)
    mp.setattr(dashboard.urllib.request, "urlopen", _no_network)
    if logs is not None:
        mp.setattr(dashboard, "log", _LogSpy(logs))
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


# ────────────────── воронка автоотказов: сбой не оставляет прошлый срез ──────────────────
# Вкладка «Автоотказы / воронка» появляется по ФАКТУ существования 13_company_funnel.csv,
# а `ReportWriter.cleanup` этот файл не трогает (он чужой для реестра отчётов). Значит,
# при сбое воронки уцелевший файл прошлого прогона показывал бы старые числа без единого
# признака устаревания — тот же класс, что закрытая для отчётов находка 24.


@pytest.fixture(scope="module")
def funnel_failure(tmp_path_factory):
    """Сборка, в которой воронка упала поверх CSV ПРОШЛОГО прогона."""
    out = tmp_path_factory.mktemp("dash_funnel_fail")
    stale = out / "reports" / "13_company_funnel.csv"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("Компания,Откликов\nПрошлый прогон,999\n", encoding="utf-8-sig")

    def _crm_unavailable(_path):
        raise RuntimeError("CRM-состояние недоступно")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dashboard, "_write_funnel_csv", _crm_unavailable)
        html = _build(mp, out, _multi_source_records())
    return SimpleNamespace(html=html, stale=stale)


def test_failed_funnel_removes_the_previous_run_csv(funnel_failure):
    assert funnel_failure.stale.exists() is False


def test_failed_funnel_hides_the_funnel_tab(funnel_failure):
    # Честно исчезнуть лучше, чем показать прошлый срез как сегодняшний.
    assert 'id="pane-all-company_funnel"' not in funnel_failure.html


# ───────────── по-портальные каталоги: пропавший портал не оставляет мусора ─────────────
# `run_reports` приходит только в каталоги источников, которые ЕСТЬ в данных, а
# `ReportWriter.cleanup` чистит лишь тот каталог, в который писал. Портал, выпавший из
# выдачи, иначе навсегда оставляет свои CSV в reports/by_source/<портал>/.


@pytest.fixture(scope="module")
def stale_source_dirs(tmp_path_factory):
    """Сборка поверх каталогов двух порталов, которых в данных уже нет."""
    out = tmp_path_factory.mktemp("dash_stale_src")
    by_source = out / "reports" / "by_source"
    (by_source / "vanished").mkdir(parents=True, exist_ok=True)
    (by_source / "vanished" / "01_cities.csv").write_text(
        "Город,Вакансий\nМосква,777\n", encoding="utf-8-sig")
    (by_source / "vanished" / "notes.txt").write_text("ручная выгрузка", encoding="utf-8")
    (by_source / "vanished_bare").mkdir(parents=True, exist_ok=True)
    (by_source / "vanished_bare" / "12_sources.csv").write_text(
        "Портал,Вакансий\nvanished_bare,1\n", encoding="utf-8-sig")
    with pytest.MonkeyPatch.context() as mp:
        _build(mp, out, _multi_source_records())
    return by_source


def test_report_of_vanished_portal_is_removed(stale_source_dirs):
    assert (stale_source_dirs / "vanished" / "01_cities.csv").exists() is False


def test_foreign_file_of_vanished_portal_survives(stale_source_dirs):
    # Чистим ТОЛЬКО свои имена (`reporter.py::MANAGED_CSV`): в каталоге может лежать
    # ручная выгрузка владельца, и снос каталога целиком её бы уничтожил.
    assert (stale_source_dirs / "vanished" / "notes.txt").read_text(
        encoding="utf-8") == "ручная выгрузка"


def test_emptied_dir_of_vanished_portal_is_dropped(stale_source_dirs):
    # Ничего чужого не осталось -> каталог тоже уходит.
    assert (stale_source_dirs / "vanished_bare").exists() is False


def test_present_portal_keeps_its_reports(stale_source_dirs):
    # Живой портал этим же прогоном переоткрыт — его срез на месте.
    assert (stale_source_dirs / "hh" / "01_cities.csv").exists() is True


# ───────────────────── сводка воронки в логе называет свой знаменатель ─────────────────────


_FUNNEL_OVERALL = {"applied": 12, "rejected": 5, "measured": 3, "le_1h": 2,
                   "invited": 4, "median_reject_min": 17}


@pytest.fixture(scope="module")
def funnel_summary_lines(tmp_path_factory):
    """Строки сводки воронки из лога сборки. overall задан ЛИТЕРАЛОМ и разными числами:
    на реальном пустом CRM все они нули, и перепутанные местами не различались бы."""
    out = tmp_path_factory.mktemp("dash_funnel_log")
    logs = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dashboard, "_write_funnel_csv", lambda _path: dict(_FUNNEL_OVERALL))
        _build(mp, out, _multi_source_records(), logs=logs)
    return [tmpl.format(*args) for _lvl, tmpl, args in logs if tmpl.startswith("Воронка:")]


def test_funnel_summary_names_the_denominator_of_the_one_hour_share(funnel_summary_lines):
    # Находка 52: доля «<=1 ч» считается от ИЗМЕРЕННЫХ отказов (так в CSV и в шапке графика),
    # а сводка писала «отказов 5 (<=1ч 2)» — читалось как доля от всех отказов. «Позитив/
    # в работе» — по той же причине, что в CSV: в INVITED_STATES входит CONSIDER.
    assert funnel_summary_lines == [
        "Воронка: откликов 12, отказов 5 (измерено 3, из них <=1ч 2), "
        "позитив/в работе 4, медиана отказа 17 мин",
    ]


# ───────────────────── локальная копия plotly.js: аудит 08.08.2026, п.58 ─────────────────────


def test_plotly_copy_is_written_under_the_current_data_dir(tmp_path, monkeypatch):
    """Загрузка ложится в ТЕКУЩИЙ DATA_DIR. Раньше путь считался на импорте от настоящего
    каталога, поэтому тест с подменённым data/ качал 3,5 МБ в личные данные владельца."""
    payload = b"x" * 1_000_001            # больше порога «файл целый» (1 МБ)

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(dashboard, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", lambda *_a, **_kw: _Resp())
    assert dashboard._ensure_plotly() == "plotly-2.35.2.min.js"
    assert (tmp_path / "plotly-2.35.2.min.js").read_bytes() == payload
