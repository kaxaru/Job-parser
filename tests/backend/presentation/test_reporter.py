"""ReportWriter: запись CSV-отчётов и чистка устаревших файлов прогона.

Защищаемые свойства: отчёт, не собравшийся в этом прогоне, не оставляет файл ПРОШЛОГО
прогона — ни в общем каталоге, ни в по-портальном; чистка трогает только СВОИ файлы
(каталог вывода приходит извне и может содержать что угодно); полный прогон пишет ровно
заявленный набор отчётов.
"""
import csv
import datetime
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net import rates
from hrwork.presentation.views import reporter

# Все отчёты реестра `_REPORTS` — литерал по спецификации docs/feed.md («Отчёты»:
# 01_cities.csv … 14_employment_by_source.csv; 13-го тут нет — воронку пишет `funnel.py`
# мимо реестра, у компаний два файла). Тот же литерал служит стражем
# согласованности для `reporter.MANAGED_CSV`: одна сторона сравнения обязана быть
# литералом, иначе тест повторил бы реализацию.
_ALL_REPORTS = {
    "01_cities.csv", "02_tech_by_city.csv", "03_salary_by_lang.csv",
    "04_salary_city_lang.csv", "05_top_stacks.csv", "06_salary_by_exp.csv",
    "07_python_stack.csv", "08_js_stack.csv", "09_remote_by_city.csv",
    "10_freshness_by_city.csv", "11_companies.csv", "11c_company_sizes.csv",
    "12_sources.csv", "14_employment_by_source.csv",
}


@pytest.fixture(autouse=True)
def _stub_rates(monkeypatch):
    """FX не из сети: run_reports строит Analyzer через with_live_rates."""
    monkeypatch.setattr(rates, "get_rates", lambda: {"USD": 1.0, "RUB": 100.0})


def _vac(vid: str, source: str) -> Vacancy:
    created = (datetime.datetime.now(tz=datetime.timezone.utc)
               - datetime.timedelta(days=10)).isoformat()
    return Vacancy(
        id=vid, name="Python-разработчик", city="Москва", city_id="1",
        salary=Salary(100_000, None, "RUR"), experience=Experience.from_code("between1And3"),
        schedule=Schedule.from_code("remote"), techs=["Python", "JavaScript", "Docker"],
        role=Role.DEVELOPER, employer="Acme", created_at=created, source=source,
    )


def _names(path) -> set[str]:
    return {p.name for p in path.iterdir()}


# --- чистка работает в ЛЮБОМ каталоге вывода, а не только в дефолтном ---

def test_stale_report_is_removed_from_per_source_dir(tmp_path):
    # ИНЦИДЕНТ 08.08.2026: cleanup молча выходил, если out_dir != REPORTS_DIR. У портала,
    # чей отчёт в этом прогоне не собрался, оставался файл ПРОШЛОГО прогона — вкладка
    # дашборда показывала старые числа без единого признака устаревания.
    out = tmp_path / "by_source" / "hh"
    out.mkdir(parents=True)
    (out / "12_sources.csv").write_text("старьё", encoding="utf-8")
    w = reporter.ReportWriter(out)
    w.csv("01_cities.csv", ["Город", "Вакансий"], [["Москва", 1]])
    w.cleanup()
    assert _names(out) == {"01_cities.csv"}


def test_report_written_this_run_is_kept(tmp_path):
    w = reporter.ReportWriter(tmp_path)
    w.csv("01_cities.csv", ["Город", "Вакансий"], [["Москва", 1]])
    w.csv("11_companies.csv", ["Компания", "Вакансий"], [["Acme", 1]])
    w.cleanup()
    assert _names(tmp_path) == {"01_cities.csv", "11_companies.csv"}


@pytest.mark.parametrize("foreign", [
    "13_company_funnel.csv",     # воронку пишет funnel.py, а не ReportWriter
    "notes.csv",
    "readme.txt",
    "dashboard.html",
])
def test_file_the_writer_does_not_own_survives_cleanup(tmp_path, foreign):
    # out_dir приходит извне и может оказаться каталогом с чужими данными: удаляем
    # ТОЛЬКО имена из своего реестра, всё остальное неприкосновенно
    (tmp_path / foreign).write_text("чужое", encoding="utf-8")
    w = reporter.ReportWriter(tmp_path)
    w.csv("01_cities.csv", ["Город", "Вакансий"], [["Москва", 1]])
    w.cleanup()
    assert _names(tmp_path) == {"01_cities.csv", foreign}


def test_subdirectory_survives_cleanup(tmp_path):
    # reports/by_source/ лежит ВНУТРИ общего каталога отчётов — чистка общего среза
    # не имеет права его тронуть
    (tmp_path / "by_source").mkdir()
    w = reporter.ReportWriter(tmp_path)
    w.csv("01_cities.csv", ["Город", "Вакансий"], [["Москва", 1]])
    w.cleanup()
    assert _names(tmp_path) == {"01_cities.csv", "by_source"}


def test_cleanup_of_never_created_dir_is_noop(tmp_path):
    # прогон не записал ни одного отчёта (каталога ещё нет) -> чистка молчит, а не падает
    out = tmp_path / "by_source" / "hirify"
    reporter.ReportWriter(out).cleanup()
    assert out.exists() is False


# --- страж: реестр отчётов и список «своих» файлов не разъезжаются ---

def test_full_run_writes_every_declared_report(tmp_path):
    vacs = [_vac("1", "hh"), _vac("2", "hirify")]      # два портала -> отчёт 12 условие проходит
    reporter.run_reports(vacs, out_dir=tmp_path)
    assert _names(tmp_path) == _ALL_REPORTS


def test_managed_csv_lists_exactly_the_reports_writer_produces():
    # страж согласованности: имя, добавленное в реестр отчётов, но забытое в MANAGED_CSV,
    # перестало бы чиститься и тихо показывало бы числа прошлого прогона
    assert reporter.MANAGED_CSV == _ALL_REPORTS


# --- страж: имена CSV-отчётов не разъезжаются между вкладками, читателем и реестром ---
# Имя отчёта живёт в ТРЁХ местах: `dashboard.py::_CHARTS` (по нему Дашборд проверяет наличие
# файла -> показывать вкладку), `charts.py::_read` (по нему график открывает файл) и
# `reporter.py::MANAGED_CSV` (что чистит ReportWriter). Страж был только у пары
# reporter↔писатель. Читаем ИСХОДНИКИ текстом (как test_feed_bridge.py), а не импортируем
# соседей: страж обязан работать без импорта plotly.
_VIEWS_DIR = Path(reporter.__file__).parent
_DASHBOARD_SRC = (_VIEWS_DIR / "dashboard.py").read_text(encoding="utf-8")
_CHARTS_SRC = (_VIEWS_DIR / "charts.py").read_text(encoding="utf-8")
# Имя отчёта — строка в кавычках, начинающаяся с цифры: `"01_cities.csv"`, `'13_...'`.
_CSV_NAME_RX = re.compile(r"['\"](\d[\w]*\.csv)['\"]")


def test_chart_csv_names_stay_within_the_reports_the_writer_owns():
    """Имя, разошедшееся между `_CHARTS` и `charts.py`, обязано быть видно страху.

    Переименование в `charts.py` без правки `_CHARTS` -> `_read` открывает несуществующий
    файл (`open` без try) и `hh.py dashboard` падает; обратный дрейф -> вкладка пропадает
    молча: «данные есть» проверяется по существованию CSV (аудит `2026-09-22-quality.md`, §3.2).
    Разрешённый набор — литерал `_ALL_REPORTS` плюс воронка: `13_company_funnel.csv` пишет
    `funnel.py` мимо реестра отчётов (см. `funnel.py::write_funnel_csv`)."""
    allowed = _ALL_REPORTS | {"13_company_funnel.csv"}
    charts_names = set(_CSV_NAME_RX.findall(_CHARTS_SRC))
    dashboard_names = set(_CSV_NAME_RX.findall(_DASHBOARD_SRC))
    assert charts_names <= allowed, f"charts.py читает чужое имя: {sorted(charts_names - allowed)}"
    assert dashboard_names <= allowed, \
        f"_CHARTS гейтит вкладку по чужому имени: {sorted(dashboard_names - allowed)}"
    # Множества равны: каждый график читает CSV, и на каждый такой CSV есть гейт вкладки.
    # Переименование в ОДНОМ месте на другое, тоже разрешённое, ловится только здесь.
    assert charts_names == dashboard_names


# --- отчёт 14: заголовки CSV — подписи форм из домена (контракт с charts.py) ---

def test_employment_report_names_columns_with_domain_labels(tmp_path):
    """Заголовки читает `charts.py::chart_employment` строгим `Employment.from_label`.
    Ожидаемое — литералы из спеки («ТК РФ/РБ, Самозанятый, ИП, ГПХ»), а не генерация из
    enum: иначе тест повторил бы реализацию и разрешил любое переименование."""
    import csv as _csv

    from hrwork.domain.employment import Employment
    vacs = [
        Vacancy(id="1", name="x", city="М", city_id="1", salary=None, experience=None,
                schedule=Schedule.OFFICE, source="hh",
                employment=(Employment.LABOR_CODE, Employment.SELF_EMPLOYED)),
        Vacancy(id="2", name="x", city="М", city_id="1", salary=None, experience=None,
                schedule=Schedule.OFFICE, source="hh", employment=()),
    ]
    reporter.run_reports(vacs, out_dir=tmp_path)
    with open(tmp_path / "14_employment_by_source.csv", encoding="utf-8-sig") as f:
        header, row = list(_csv.reader(f))[:2]
    assert header == ["Портал", "Вакансий", "ТК РФ/РБ", "Самозанятый", "ИП", "ГПХ",
                      "Не указано", "%названо"]
    # вакансия с двумя формами считается в обоих столбцах, но «названо» у неё одно
    assert row == ["hh", "2", "1", "1", "0", "0", "1", "50.0"]


# --- отчёт 6: подписи корзин приходят из config.EXP_LABELS, а не из четырёх литералов ---

def test_salary_by_exp_rows_take_their_names_from_the_exp_labels_dict(tmp_path, monkeypatch):
    """Переименование подписи в `config.EXP_LABELS` НЕ должно тихо терять строку отчёта.

    До 23.09.2026 `reporter.py::report_salary_by_exp` держал `order` четырьмя литералами, тогда
    как ключи бакетов приходят из ТОГО ЖЕ словаря (`config.EXP_LABELS` -> `Experience.label` ->
    `analyzer.py::salary_by_experience`). Подпись, уехавшая в словаре, переставала находить свой
    бакет, и строка «6. Зарплата по опыту» исчезала БЕЗ ошибки и предупреждения
    (аудит `2026-09-22-quality.md`, §3.2). Здесь подпись одного грейда новая — строка обязана
    остаться; на литералах `rows` пуст и файл содержит только шапку."""
    monkeypatch.setattr(reporter, "EXP_LABELS", {
        "noExperience": "Без опыта", "between1And3": "1–3 года (новая подпись)",
        "between3And6": "3–6 лет", "moreThan6": "6+ лет"})
    analyzer = SimpleNamespace(salary_by_experience=lambda: {
        "1–3 года (новая подпись)": {"n": 5, "median": 120_000, "p25": 100_000, "p75": 140_000}})
    reporter.report_salary_by_exp(analyzer, reporter.ReportWriter(tmp_path))
    with open(tmp_path / "06_salary_by_exp.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [
        ["Опыт", "N", "Медиана", "P25", "P75"],
        ["1–3 года (новая подпись)", "5", "120 000", "100 000", "140 000"],
    ]
