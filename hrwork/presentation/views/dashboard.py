"""Сборка интерактивного HTML-дашборда (Jinja2 + Plotly)."""
import csv
import datetime
import shutil

import plotly.io as pio
from jinja2 import Environment, FileSystemLoader

from hrwork.application.funnel import write_funnel_csv as _write_funnel_csv
from hrwork.config import (
    ACCENT,
    BG,
    DASHBOARD_OUT,
    DATA_DIR,
    GRID,
    PAPER,
    REPORTS_DIR,
    TEMPLATE_DIR,
    TEXT,
    log,
)
from hrwork.infrastructure.storage import vacancy_repository

from . import reporter as _reporter
from .charts import (
    chart_by_source,
    chart_cities,
    chart_companies,
    chart_companies_remote,
    chart_companies_sizes,
    chart_company_funnel,
    chart_freshness,
    chart_heatmap_city_lang,
    chart_js_stack,
    chart_python_stack,
    chart_remote,
    chart_salary_exp,
    chart_salary_lang,
    chart_stacks,
    chart_tech_heatmap,
)

# key -> (chart fn, подпись вкладки, CSV-файл для проверки наличия по источнику)
_CHARTS = {
    "cities":           (chart_cities,            "Города",                "01_cities.csv"),
    "salary_lang":      (chart_salary_lang,       "Зарплата по языку",     "03_salary_by_lang.csv"),
    "salary_exp":       (chart_salary_exp,        "Зарплата по опыту",     "06_salary_by_exp.csv"),
    "stacks":           (chart_stacks,            "Пары языков",           "05_top_stacks.csv"),
    "hm_city_lang":     (chart_heatmap_city_lang, "Город x язык",          "04_salary_city_lang.csv"),
    "hm_tech":          (chart_tech_heatmap,      "Технологии по городам", "02_tech_by_city.csv"),
    "remote":           (chart_remote,            "Удалёнка",              "09_remote_by_city.csv"),
    "py_stack":         (chart_python_stack,      "Python стек",           "07_python_stack.csv"),
    "js_stack":         (chart_js_stack,          "JS стек",               "08_js_stack.csv"),
    "freshness":        (chart_freshness,         "Свежесть / гост",       "10_freshness_by_city.csv"),
    "companies":        (chart_companies,         "Компании",              "11_companies.csv"),
    "companies_all":    (chart_companies_sizes,   "Все работодатели",      "11c_company_sizes.csv"),
    "companies_remote": (chart_companies_remote,  "Удалёнка: компании",    "11_companies.csv"),
    "company_funnel":   (chart_company_funnel,    "Автоотказы / воронка",  "13_company_funnel.csv"),
    "by_source":        (chart_by_source,         "Порталы",               "12_sources.csv"),
}

SOURCE_LABELS = {"all": "Все"}      # hh/hirify берут своё имя как есть


def build_dashboard() -> None:
    # Грузим вакансии для по-портальных срезов отчётов (фильтр источника в дашборде).
    vacs = [r.vacancy for r in vacancy_repository().load()]
    present = sorted({v.source for v in vacs})             # напр. ['hh', 'hirify']
    src_dirs = {"all": REPORTS_DIR}
    if len(present) > 1:                                    # срез по источнику нужен только у агрегатора
        for src in present:
            d = REPORTS_DIR / "by_source" / src
            _reporter.run_reports([v for v in vacs if v.source == src], out_dir=d)
            src_dirs[src] = d
    _reporter.run_reports(vacs, out_dir=REPORTS_DIR)        # 'all' последним -> дефолтный каталог
    sources = ["all"] + (list(present) if len(present) > 1 else [])

    # Воронка автоотказов — CRM-данные (отклики/чаты), НЕ рыночные Vacancy, поэтому пишется
    # отдельно от run_reports: строго после его cleanup() (иначе снесёт как «неучтённый» CSV)
    # и только в общий срез (отклики не делятся по источнику). Пусто/нет данных -> нет вкладки.
    try:
        ov = _write_funnel_csv(REPORTS_DIR / "13_company_funnel.csv")
        log.info("Воронка: откликов {}, отказов {} (<=1ч {}), приглашений {}, медиана отказа {} мин",
                 ov["applied"], ov["rejected"], ov["le_1h"], ov["invited"], ov["median_reject_min"])
    except Exception as e:                                  # CRM-состояние может отсутствовать
        log.warning("Воронку автоотказов пропускаю: {}", e)

    # Набор и порядок вкладок = _CHARTS (единый реестр), чьи CSV есть в общем срезе. Условные
    # (freshness/companies/by_source) появляются только когда собран соответствующий отчёт.
    keys = [k for k, (_fn, _lbl, csvf) in _CHARTS.items() if (REPORTS_DIR / csvf).exists()]
    labels = {k: _CHARTS[k][1] for k in keys}

    # Рендерим фигуры по каждому источнику; вариант появляется только если его CSV существует.
    variants: dict[str, dict[str, str]] = {}
    for src in sources:
        base = src_dirs[src]                               # каталог CSV этого среза — передаём явно
        vd: dict[str, str] = {}
        for k in keys:
            _fn, _lbl, csvf = _CHARTS[k]
            if k == "by_source" and src != "all":
                continue                                   # «Порталы» — только в общем срезе
            if not (base / csvf).exists():
                continue                                   # у источника нет данных для графика
            vd[k] = pio.to_html(_fn(base), full_html=False, include_plotlyjs=False,
                                div_id=f"fig_{src}_{k}", config={"responsive": True})
        variants[src] = vd

    # Динамическая статистика — без хардкода
    with open(REPORTS_DIR / "01_cities.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    total_vacancies = f"{sum(int(r['Вакансий']) for r in rows):,}".replace(",", " ")
    total_cities = len(rows)

    source_labels = {s: SOURCE_LABELS.get(s, s) for s in sources}
    env  = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. CSS (Jinja2 с цветовыми переменными)
    css = env.get_template("dashboard.css.j2").render(
        bg=BG, paper=PAPER, grid=GRID, text=TEXT, accent=ACCENT,
    )
    (DATA_DIR / "dashboard.css").write_text(css, encoding="utf-8")
    log.info("dashboard.css сохранён  ({} байт)", len(css))

    # 2. JS (статика из src/ — копируем как есть; не Jinja, поэтому не в templates/)
    shutil.copy(TEMPLATE_DIR.parent / "src" / "dashboard.js", DATA_DIR / "dashboard.js")
    log.info("dashboard.js скопирован")

    # 3. HTML (структура + Plotly-дивы)
    html = env.get_template("dashboard.html.j2").render(
        keys=keys,
        labels=labels,
        sources=sources,
        source_labels=source_labels,
        variants=variants,
        first_key=keys[0],
        first_source="all",
        total_vacancies=total_vacancies,
        total_cities=total_cities,
        year=datetime.date.today().year,
    )

    DASHBOARD_OUT.write_text(html, encoding="utf-8")
    log.success("Dashboard -> {}  (вакансий: {}, городов: {})",
                DASHBOARD_OUT, total_vacancies, total_cities)
