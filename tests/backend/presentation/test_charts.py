"""Тесты построителя Plotly-графиков: агрегация/нормировка на маленьких CSV-срезах.

charts.py читает по-портальные CSV-отчёты (_read(base, name)) и лепит из них go.Figure.
«Малый in-memory вход» для этого модуля — крохотный CSV в tmp_path; чистые хелперы
(_num, _stacked_pct_bars) берут аргументы напрямую. Ни сети, ни REPORTS_DIR — base
всегда tmp_path. Проверяем СВОЙСТВА: разбор чисел, доли внутри строки, порядок/реверс,
top-N, поведение на пустом отчёте.
"""
import csv

import plotly.graph_objects as go
import pytest

from hrwork.presentation.views.charts import (
    _num,
    _stacked_pct_bars,
    chart_by_source,
    chart_cities,
    chart_companies,
    chart_company_funnel,
    chart_freshness,
    chart_remote,
    chart_tech_heatmap,
)

_FUNNEL_HEADER = ["Компания", "Откликов", "Отказов", "Измерено", "<=10м", "<=1ч",
                  "<=1д", "Позитив/в работе", "Медиана мин", "Автобан % (от измеренных)"]


def _write_csv(base, name: str, header: list[str], rows: list[list]) -> None:
    """Пишет CSV-отчёт так же, как их читает _read (utf-8, DictReader по заголовку)."""
    with open(base / name, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# --- _num: локализованный разбор чисел из CSV (nbsp/пробел-разделитель, запятая-точка) ---

@pytest.mark.parametrize(("raw", "expected"), [
    ("100", 100.0),
    ("1 000", 1000.0),               # обычный пробел-разделитель тысяч выкидывается
    ("1 000", 1000.0),          # неразрывный пробел тоже
    ("1,5", 1.5),                    # десятичная запятая -> точка
    ("  200 000,50  ", 200000.5),    # обрезка + разделители + запятая вместе
])
def test_num_parses_thousands_and_decimal_separators(raw, expected):
    assert _num(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "—", "abc", "12x"])
def test_num_yields_none_for_blank_dash_or_garbage(raw):
    # пустое/прочерк/нечисло -> None (в графике станет 0 или дырой), а не исключение
    assert _num(raw) is None


# --- _stacked_pct_bars: доля каждого сегмента считается от суммы ПЕРЕДАННЫХ рядов ---

def test_stacked_pct_bars_labels_each_segment_with_its_share_of_the_row():
    fig = _stacked_pct_bars(
        "t", ["A", "B"],
        [("Удал", [30, 10], "#54A24B"),
         ("Офис", [10, 10], "#4C78A8")],
        noun="вакансий", xaxis_title="x",
    )
    # база доли на строку A = 30+10=40, на строку B = 10+10=20
    assert fig.data[0].name == "Удал"
    assert list(fig.data[0].text) == ["75%", "50%"]   # 30/40, 10/20
    assert list(fig.data[1].text) == ["25%", "50%"]   # 10/40, 10/20
    # нормировка стека к 100% задаётся в layout, а не подгоняется вручную
    assert fig.layout.barmode == "stack"
    assert fig.layout.barnorm == "percent"


def test_stacked_pct_bars_zero_total_row_is_zero_percent_not_error():
    # строка без данных (сумма рядов = 0) -> 0%, деления на ноль нет
    fig = _stacked_pct_bars(
        "t", ["Z"],
        [("Удал", [0], "#54A24B"), ("Офис", [0], "#4C78A8")],
        noun="вакансий", xaxis_title="x",
    )
    assert list(fig.data[0].text) == ["0%"]
    assert list(fig.data[1].text) == ["0%"]


# --- chart_cities: реверс входа, чтобы крупнейший город рисовался сверху ---

def test_cities_bar_is_reversed_so_largest_renders_on_top(tmp_path):
    _write_csv(tmp_path, "01_cities.csv", ["Город", "Вакансий"],
               [["Москва", 100], ["СПб", 60], ["Казань", 20]])
    fig = chart_cities(tmp_path)
    # CSV идёт по убыванию; на гориз. барах последний y — верхний, поэтому вход реверсится
    assert list(fig.data[0].y) == ["Казань", "СПб", "Москва"]
    assert list(fig.data[0].x) == [20, 60, 100]
    assert list(fig.data[0].text) == ["20", "60", "100"]   # подпись = значение (plotly -> str)


# --- chart_by_source: подпись несёт долю удалёнки, ноль-тотал не делит на ноль ---

def test_by_source_label_shows_remote_share_and_guards_zero_total(tmp_path):
    _write_csv(tmp_path, "12_sources.csv", ["Портал", "Вакансий", "Удалённо"],
               [["hh", 100, 40], ["empty", 0, 0]])
    fig = chart_by_source(tmp_path)
    assert list(fig.data[0].x) == ["hh", "empty"]
    assert list(fig.data[0].y) == [100, 0]
    assert "40% удал." in fig.data[0].text[0]   # 40/100
    assert "0% удал." in fig.data[0].text[1]    # тотал 0 -> 0%, без ZeroDivisionError


# --- chart_tech_heatmap: колонки — топ технологий по СУММЕ упоминаний по городам ---

def _tech_rows():
    return [["Москва", "Python", 50], ["Москва", "Java", 10],
            ["СПб", "Python", 30], ["СПб", "Go", 5]]


def test_tech_heatmap_maps_cities_to_totals_ordered_columns(tmp_path):
    _write_csv(tmp_path, "02_tech_by_city.csv", ["Город", "Технология", "Упоминаний"],
               _tech_rows())
    fig = chart_tech_heatmap(tmp_path)
    # колонки по убыванию суммы: Python 80, Java 10, Go 5; города — по алфавиту
    assert list(fig.data[0].x) == ["Python", "Java", "Go"]
    assert list(fig.data[0].y) == ["Москва", "СПб"]
    assert [list(row) for row in fig.data[0].z] == [[50, 10, 0], [30, 0, 5]]


@pytest.mark.parametrize(("top_n", "expected"), [
    (1, ["Python"]),
    (2, ["Python", "Java"]),
    (3, ["Python", "Java", "Go"]),
])
def test_tech_heatmap_top_n_keeps_only_highest_total_techs(tmp_path, top_n, expected):
    _write_csv(tmp_path, "02_tech_by_city.csv", ["Город", "Технология", "Упоминаний"],
               _tech_rows())
    fig = chart_tech_heatmap(tmp_path, top_n=top_n)
    assert list(fig.data[0].x) == expected


# --- chart_companies: возраст в подписи только если он есть; реверс топа ---

def test_companies_bar_appends_age_only_when_present_and_reverses_order(tmp_path):
    _write_csv(tmp_path, "11_companies.csv",
               ["Компания", "Вакансий", "Медиана возраста"],
               [["A", 100, 10], ["B", 50, "—"], ["C", 20, 0]])
    fig = chart_companies(tmp_path)
    assert list(fig.data[0].y) == ["C", "B", "A"]     # реверс: топ-компания сверху
    assert fig.data[0].text[0] == "20"                # возраст 0 -> без «Nд»
    assert fig.data[0].text[1] == "50"                # прочерк -> без «Nд»
    assert "10д" in fig.data[0].text[2]               # возраст есть -> дописан


def test_companies_bar_caps_at_top_25(tmp_path):
    rows = [[f"C{i}", 100 - i, 10] for i in range(30)]
    _write_csv(tmp_path, "11_companies.csv",
               ["Компания", "Вакансий", "Медиана возраста"], rows)
    fig = chart_companies(tmp_path)
    assert len(fig.data[0].y) == 25                   # rows[:25], хвост отброшен


# --- chart_remote: сорт городов по возрастанию «Всего» + нормировка доли ---

def test_remote_sorts_cities_ascending_by_total_and_normalizes_share(tmp_path):
    _write_csv(tmp_path, "09_remote_by_city.csv",
               ["Город", "Всего", "Удалённо", "Офис"],
               [["Москва", 100, 60, 40], ["СПб", 50, 20, 30]])
    fig = chart_remote(tmp_path)
    # asc по «Всего»: СПб(50) -> Москва(100); в plotly последний y сверху -> Москва наверху
    assert list(fig.data[0].y) == ["СПб", "Москва"]
    assert fig.data[0].name == "Удалённо"
    assert list(fig.data[0].text) == ["40%", "60%"]   # 20/50, 60/100


# --- chart_company_funnel: доля «<=1ч» в шапке считается от ИЗМЕРЕННЫХ, знаменатель назван ---

def test_company_funnel_title_states_measured_denominator(tmp_path):
    # БАГ 08.08.2026: шапка делила «<=1ч» на ВСЕ отказы, а колонка CSV «Автобан %» —
    # на «Измерено». Компания с 10 отказами, из них 2 с чат-меткой и обе <=1ч, давала
    # «100.0» в таблице и «20%» в шапке ТОЙ ЖЕ вкладки. Знаменатель один — измеренные,
    # и он назван прямо в подписи: человек, видящий только график, не должен гадать.
    _write_csv(tmp_path, "13_company_funnel.csv", _FUNNEL_HEADER,
               [["ACME", 12, 10, 2, 0, 2, 2, 0, 30, 100.0]])
    fig = chart_company_funnel(tmp_path)
    assert fig.layout.title.text == ("Латентность автоотказа по компаниям "
                                     "(отказов 10, измерено 2, из них <=1ч — 2 = 100%)")


def test_company_funnel_unmeasured_rejects_go_to_no_mark_segment(tmp_path):
    # «Отказов - Измерено» = молчаливые DISCARD без письма: серый сегмент «без метки»,
    # а не «медленный отказ» — иначе знаменатель шапки и длина баров рассказывали бы разное
    _write_csv(tmp_path, "13_company_funnel.csv", _FUNNEL_HEADER,
               [["ACME", 12, 10, 2, 1, 2, 2, 0, 30, 100.0]])
    seg = {t.name: list(t.x) for t in chart_company_funnel(tmp_path).data}
    assert seg["<=10 мин"] == [1]
    assert seg["10-60 мин"] == [1]                 # 2 - 1
    assert seg["1 ч-1 день"] == [0]                # 2 - 2
    assert seg[">1 дня"] == [0]                    # измерено 2 - <=1д 2
    assert seg["без метки"] == [8]                 # отказов 10 - измерено 2


def test_company_funnel_title_without_measured_rejects_is_zero_percent(tmp_path):
    # все отказы молчаливые: доля от нуля измеренных -> 0%, без ZeroDivisionError
    _write_csv(tmp_path, "13_company_funnel.csv", _FUNNEL_HEADER,
               [["ACME", 5, 4, 0, 0, 0, 0, 0, "", 0.0]])
    fig = chart_company_funnel(tmp_path)
    assert fig.layout.title.text == ("Латентность автоотказа по компаниям "
                                     "(отказов 4, измерено 0, из них <=1ч — 0 = 0%)")


# --- пустой отчёт: любая chart_*-функция отдаёт валидную go.Figure, а не падает ---

@pytest.mark.parametrize(("func", "name", "header"), [
    (chart_cities, "01_cities.csv", ["Город", "Вакансий"]),
    (chart_by_source, "12_sources.csv", ["Портал", "Вакансий", "Удалённо"]),
    (chart_tech_heatmap, "02_tech_by_city.csv", ["Город", "Технология", "Упоминаний"]),
    (chart_companies, "11_companies.csv", ["Компания", "Вакансий", "Медиана возраста"]),
    (chart_remote, "09_remote_by_city.csv", ["Город", "Всего", "Удалённо", "Офис"]),
    (chart_freshness, "10_freshness_by_city.csv",
     ["Город", "Всего", "Свежих", "30-60дн", "Гостов"]),
    (chart_company_funnel, "13_company_funnel.csv", _FUNNEL_HEADER),
])
def test_chart_returns_figure_on_empty_report(tmp_path, func, name, header):
    _write_csv(tmp_path, name, header, [])   # только заголовок -> _read вернёт []
    assert isinstance(func(tmp_path), go.Figure)
