"""ReportWriter: запись CSV-отчётов и чистка устаревших файлов прогона.

Защищаемые свойства: отчёт, не собравшийся в этом прогоне, не оставляет файл ПРОШЛОГО
прогона — ни в общем каталоге, ни в по-портальном; чистка трогает только СВОИ файлы
(каталог вывода приходит извне и может содержать что угодно); полный прогон пишет ровно
заявленный набор отчётов.
"""
import datetime

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net import rates
from hrwork.presentation.views import reporter

# Все отчёты реестра `_REPORTS` — литерал по спецификации docs/feed.md («Отчёты»:
# 01_cities.csv … 12_sources.csv, у компаний два файла). Тот же литерал служит стражем
# согласованности для `reporter.MANAGED_CSV`: одна сторона сравнения обязана быть
# литералом, иначе тест повторил бы реализацию.
_ALL_REPORTS = {
    "01_cities.csv", "02_tech_by_city.csv", "03_salary_by_lang.csv",
    "04_salary_city_lang.csv", "05_top_stacks.csv", "06_salary_by_exp.csv",
    "07_python_stack.csv", "08_js_stack.csv", "09_remote_by_city.csv",
    "10_freshness_by_city.csv", "11_companies.csv", "11c_company_sizes.csv",
    "12_sources.csv",
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
