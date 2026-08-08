"""Сборка интерактивного HTML-дашборда (Jinja2 + Plotly)."""
import csv
import datetime
import shutil
import urllib.request
from pathlib import Path

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

SOURCE_LABELS = {"all": "Все"}      # порталы берут своё имя как есть

# plotly.js держим РЯДОМ с дашбордом, а не тянем с CDN на каждое открытие: замер 03.08.2026
# показал 7.96 с из 8.03 с до первого графика — это скачивание ~3.5 МБ. Раньше цена пряталась
# за отрисовкой 67 диаграмм, после ленивого рендера стала главной.
# Версия ЗАКРЕПЛЕНА: фигуры сериализуются этой же версией plotly, расхождение мажора ломает
# схему figure. Файл лежит в data/ (гитигнорен) и качается один раз при сборке; если скачать
# не удалось — шаблон падает на CDN, дашборд остаётся рабочим.
PLOTLY_VERSION = "2.35.2"
PLOTLY_CDN = f"https://cdn.plot.ly/plotly-{PLOTLY_VERSION}.min.js"
PLOTLY_FILE = f"plotly-{PLOTLY_VERSION}.min.js"


def _plotly_local() -> Path:
    """Путь локальной копии plotly.js. Функция, а не константа: `DATA_DIR` подменяем
    (тесты сборки), а константа связалась бы с настоящим каталогом на ИМПОРТЕ — и загрузка
    ушла бы в личный data/ мимо подмены (аудит 08.08.2026, п.58)."""
    return DATA_DIR / PLOTLY_FILE


def _ensure_plotly() -> str | None:
    """Скачать plotly.js рядом с дашбордом, если его там нет. Возвращает имя файла для
    относительной ссылки (работает и через `hh.py serve`, и при открытии как file://),
    либо None — тогда шаблон возьмёт CDN."""
    local = _plotly_local()
    if local.exists() and local.stat().st_size > 1_000_000:
        return local.name
    try:
        # URL фиксированный https, не из пользовательского ввода
        with urllib.request.urlopen(PLOTLY_CDN, timeout=60) as r:
            data = r.read()
        tmp = local.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(local)                         # атомарно: полуфайл не подхватится
        log.info("plotly.js скачан локально: {:.1f} МБ -> {}", len(data) / 1e6, local.name)
        return local.name
    except Exception as e:
        log.warning("plotly.js скачать не удалось ({}) — дашборд возьмёт CDN", e)
        return None


def _prune_stale_source_dirs(fresh: set[str]) -> None:
    """Снести отчёты по-портальных каталогов, которые этот прогон НЕ переоткрывал.

    Портал пропал из данных (или срез по источникам вообще не строится) -> в
    `reports/by_source/<портал>/` никто больше не приходит: `run_reports` зовут только для
    живых источников, а `ReportWriter.cleanup` чистит лишь тот каталог, в который писал.
    Это последний путь к устаревшим CSV в дереве отчётов после фикса находки 24.

    Удаляем ТОЛЬКО СВОЁ — имена из `reporter.py::MANAGED_CSV`. Посторонний файл (ручная
    выгрузка, заметка) обязан пережить чистку, поэтому каталог снимается лишь опустевшим."""
    base = REPORTS_DIR / "by_source"
    if not base.is_dir():
        return
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name in fresh:
            continue
        stale = [p for p in sorted(d.iterdir())
                 if p.is_file() and p.name in _reporter.MANAGED_CSV]
        for path in stale:
            path.unlink()
        if not any(d.iterdir()):
            d.rmdir()                              # остались чужие файлы -> каталог не трогаем
        if stale:
            log.info("Портал {} пропал из данных — снято устаревших отчётов: {}",
                     d.name, len(stale))


def build_dashboard() -> None:
    # Грузим вакансии для по-портальных срезов отчётов (фильтр источника в дашборде).
    vacs = [r.vacancy for r in vacancy_repository().load()]
    present = sorted({v.source for v in vacs})   # из ДАННЫХ: напр. ['getmatch','hh','hirify']
    src_dirs = {"all": REPORTS_DIR}
    if len(present) > 1:                                    # срез по источнику нужен только у агрегатора
        for src in present:
            d = REPORTS_DIR / "by_source" / src
            _reporter.run_reports([v for v in vacs if v.source == src], out_dir=d)
            src_dirs[src] = d
    _reporter.run_reports(vacs, out_dir=REPORTS_DIR)        # 'all' последним -> дефолтный каталог
    _prune_stale_source_dirs(set(src_dirs) - {"all"})       # каталоги пропавших порталов
    sources = ["all"] + (list(present) if len(present) > 1 else [])

    # Воронка автоотказов — CRM-данные (отклики/чаты), НЕ рыночные Vacancy, поэтому пишется
    # отдельно от run_reports: строго после его cleanup() (иначе снесёт как «неучтённый» CSV)
    # и только в общий срез (отклики не делятся по источнику). Пусто/нет данных -> нет вкладки.
    funnel_csv = REPORTS_DIR / "13_company_funnel.csv"
    try:
        ov = _write_funnel_csv(funnel_csv)
        # Подписи те же, что в CSV (`funnel.py::_CSV_HEADERS`) и в шапке графика
        # (`charts.py::chart_company_funnel`): знаменатель доли «<=1 ч» — ИЗМЕРЕННЫЕ отказы,
        # а «отказов N (<=1ч M)» читалось как доля от всех. «Позитив/в работе» — по той же
        # причине, что в CSV: в INVITED_STATES входит CONSIDER, это ещё не приглашение.
        log.info("Воронка: откликов {}, отказов {} (измерено {}, из них <=1ч {}), "
                 "позитив/в работе {}, медиана отказа {} мин",
                 ov["applied"], ov["rejected"], ov["measured"], ov["le_1h"],
                 ov["invited"], ov["median_reject_min"])
    except Exception as e:                                  # CRM-состояние может отсутствовать
        # Срез ПРОШЛОГО прогона снимаем: вкладка появляется по факту существования CSV
        # (`keys` ниже), и уцелевший файл показывал бы старые числа без признака устаревания —
        # ровно то, от чего лечит `reporter.py::ReportWriter.cleanup` остальные отчёты.
        funnel_csv.unlink(missing_ok=True)
        log.warning("Воронку автоотказов пропускаю: {} (устаревший {} удалён)", e, funnel_csv.name)

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
            # to_json, а НЕ to_html: to_html оборачивает фигуру в <script>Plotly.newPlot(…),
            # и все такие блоки выполняются при загрузке страницы — 67 диаграмм разом, из
            # них 66 в скрытых панелях (13 графиков × 5 срезов). Отдаём чистые данные,
            # а строит их dashboard.js по факту показа панели.
            vd[k] = pio.to_json(_fn(base))
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
        source_count=len(present),            # порталов в выборке — для подписи в шапке
        source_labels=source_labels,
        variants=variants,
        first_key=keys[0],
        first_source="all",
        total_vacancies=total_vacancies,
        total_cities=total_cities,
        year=datetime.date.today().year,
        plotly_local=_ensure_plotly(),        # None -> шаблон возьмёт CDN
        plotly_cdn=PLOTLY_CDN,
    )

    DASHBOARD_OUT.write_text(html, encoding="utf-8")
    log.success("Dashboard -> {}  (вакансий: {}, городов: {})",
                DASHBOARD_OUT, total_vacancies, total_cities)
