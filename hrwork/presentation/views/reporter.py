"""Отчёты: консольные таблицы и CSV-файлы.

`run_reports` — драйвер: строит Analyzer + ReportWriter и прогоняет реестр `_REPORTS`
(по функции на отчёт). Каталог вывода и множество записанных файлов держит ReportWriter
(инстанс на прогон) — раньше это были модульные globals `_OUT_DIR`/`_written`, из-за
которых по-портальные срезы дашборда рисковали гонкой.
"""
import csv
from pathlib import Path
from typing import Any

from hrwork.application.analyzer import Analyzer
from hrwork.config import REPORTS_DIR, log
from hrwork.domain import freshness
from hrwork.domain.models import Vacancy


def _fmt(n: int | None) -> str:
    return f'{n:,}'.replace(',', ' ') if n is not None else '—'


class ReportWriter:
    """Вывод отчётов в конкретный каталог. Инстанс на прогон -> нет общих globals/гонки."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.written: set[str] = set()   # имена CSV этого прогона (для чистки устаревших)

    def table(self, headers: list[Any], rows: list[Any], title: str = '') -> None:
        lines = []
        if title:
            lines += [f'\n{"=" * 72}', f'  {title}', '=' * 72]
        if not rows:
            lines.append('  (нет данных)')
            log.info('\n{}', '\n'.join(lines))
            return
        widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
        fmt = '  ' + '  '.join(f'{{:<{w}}}' for w in widths)
        lines.append(fmt.format(*headers))
        lines.append('  ' + '-' * (sum(widths) + 2 * len(widths)))
        lines.extend(fmt.format(*[str(x) for x in row]) for row in rows)
        log.info('\n{}', '\n'.join(lines))

    def csv(self, name: str, headers: list[Any], rows: list[Any]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.written.add(name)
        path = self.out_dir / name
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            csv.writer(f).writerows([headers, *rows])
        log.info('-> {}', path)

    def cleanup(self) -> None:
        """Удалить .csv, НЕ записанные в этом прогоне (отчёт стал условным и не сгенерился).
        Только в дефолтном каталоге: по-портальные подкаталоги пишутся заново, а удаление
        по чужому каталогу рисковало бы снести данные другого среза."""
        if self.out_dir != REPORTS_DIR:
            return
        for path in self.out_dir.iterdir():
            if path.is_file() and path.suffix == '.csv' and path.name not in self.written:
                path.unlink()
                log.info('Удалён устаревший отчёт: {}', path)


# ── Отчёты: по функции на отчёт (a=Analyzer, w=ReportWriter). Порядок = реестр _REPORTS. ──

def report_cities(a: Analyzer, w: ReportWriter) -> None:
    counts = a.count_by_city()
    rows = [[c, n] for c, n in counts.most_common()]
    w.table(['Город', 'Вакансий'], rows, '1. Вакансий по городам')
    w.csv('01_cities.csv', ['Город', 'Вакансий'], rows)


def report_tech_by_city(a: Analyzer, w: ReportWriter) -> None:
    counts = a.count_by_city()
    tech_city = a.tech_by_city()
    all_rows: list[list[Any]] = []
    lines = [f'\n{"=" * 72}', '  2. ТОП-10 технологий по городам', '=' * 72]
    for city, counter in sorted(tech_city.items()):
        lines.append(f'\n  {city}:')
        for tech, cnt in counter.most_common(10):
            bar = '#' * min(cnt // max(1, counts[city] // 50), 30)
            lines.append(f'    {tech:<22} {cnt:>4}  {bar}')
        for tech, cnt in counter.most_common(50):
            all_rows.append([city, tech, cnt])
    log.info('\n{}', '\n'.join(lines))
    w.csv('02_tech_by_city.csv', ['Город', 'Технология', 'Упоминаний'], all_rows)


def report_salary_by_lang(a: Analyzer, w: ReportWriter) -> None:
    sal_lang = a.salary_by_lang()
    cols = ['Язык', 'N', 'Медиана', 'P25', 'P75', 'Среднее']
    rows = sorted(
        [[lang, s['n'], _fmt(s['median']), _fmt(s['p25']), _fmt(s['p75']), _fmt(s['mean'])]
         for lang, s in sal_lang.items()],
        key=lambda x: sal_lang[x[0]]['median'], reverse=True,
    )
    w.table(cols, rows, '3. Зарплата по языкам, net RUB (все города)')
    w.csv('03_salary_by_lang.csv', cols, rows)


def report_salary_city_lang(a: Analyzer, w: ReportWriter) -> None:
    sal_lang = a.salary_by_lang()
    top_langs = sorted(sal_lang, key=lambda x: sal_lang[x]['median'], reverse=True)[:8]
    sal_cl = a.salary_city_lang()
    rows = []
    for city in sorted(sal_cl):
        row = [city]
        for lang in top_langs:
            row.append(_fmt(sal_cl[city].get(lang, {}).get('median')))
        rows.append(row)
    w.table(['Город', *top_langs], rows, '4. Медиана net RUB: город x язык')
    flat = [[city, lang, d['n'], _fmt(d['median']), _fmt(d.get('p25')), _fmt(d.get('p75'))]
            for city, langs in sal_cl.items()
            for lang, d in sorted(langs.items(), key=lambda x: x[1]['median'], reverse=True)]
    w.csv('04_salary_city_lang.csv', ['Город', 'Язык', 'N', 'Медиана', 'P25', 'P75'], flat)


def report_top_stacks(a: Analyzer, w: ReportWriter) -> None:
    rows = [[f'{t[0]} + {t[1]}', cnt] for t, cnt in a.top_stacks(20)]
    w.table(['Стек', 'Упоминаний'], rows, '5. Популярные пары языков')
    w.csv('05_top_stacks.csv', ['Стек', 'Упоминаний'], rows)


def report_salary_by_exp(a: Analyzer, w: ReportWriter) -> None:
    sal_exp = a.salary_by_experience()
    order = ['Без опыта', '1–3 года', '3–6 лет', '6+ лет']
    cols = ['Опыт', 'N', 'Медиана', 'P25', 'P75']
    rows = [[exp, s['n'], _fmt(s['median']), _fmt(s.get('p25')), _fmt(s.get('p75'))]
            for exp in order if (s := sal_exp.get(exp))]
    w.table(cols, rows, '6. Зарплата по опыту (net RUB)')
    w.csv('06_salary_by_exp.csv', cols, rows)


def report_lang_stacks(a: Analyzer, w: ReportWriter) -> None:
    # 7-8. Сопутствующий стек для Python и JavaScript
    for lang, fname, num in [('Python', '07_python_stack.csv', '07'),
                             ('JavaScript', '08_js_stack.csv', '08')]:
        rows = [[tech, cnt] for tech, cnt in a.tech_stack_for_lang(lang)]
        w.table(['Технология', 'Вакансий'], rows,
                f'{num}. Стек при {lang} (топ-25 сопутствующих технологий)')
        w.csv(fname, ['Технология', 'Вакансий'], rows)


def report_remote_by_city(a: Analyzer, w: ReportWriter) -> None:
    cols = ['Город', 'Всего', 'Удалённо', 'Офис', '%удалённо']
    rows = [[r['city'], r['total'], r['remote'], r['onsite'], r['pct']]
            for r in a.remote_by_city()]
    w.table(cols, rows, '9. Удалённая работа по городам')
    w.csv('09_remote_by_city.csv', cols, rows)


def report_freshness(a: Analyzer, w: ReportWriter) -> None:
    # 10. Свежесть вакансий (создана≤30дн = свежая; >60дн и висит = гост)
    fs = a.freshness_summary()
    if not fs['dated']:
        log.warning('Тайминга нет ни у одной вакансии — пересобери: python hh.py collect --force')
        return
    c = fs['counts']            # code-keyed: {"fresh": n, ...}
    w.table(['Класс', 'Вакансий'],
            [[fc.label, c[fc.code]] for fc in freshness.FreshnessClass],
            f"10. Свежесть (с датой: {fs['dated']}/{fs['total']}, "
            f"гостов {fs['ghost_pct']}%, медиана возраста {fs['median_age']} дн)")
    cols = ['Город', 'Всего', 'Свежих', '30-60дн', 'Гостов', '%гостов', 'Медиана возраста']
    rows = [[r['city'], r['total'], r['fresh'], r['recent'], r['ghost'], r['ghost_pct'],
             r['median_age']] for r in a.freshness_by_city()]
    w.table(cols, rows, '10b. Свежесть по городам (сорт по доле гостов)')
    w.csv('10_freshness_by_city.csv', cols, rows)


def report_companies(a: Analyzer, w: ReportWriter) -> None:
    # 11. Топ работодателей: вакансий + медиана «сколько висят»
    comp = a.by_company(30)
    if not comp:
        return
    cols = ['Компания', 'Вакансий', 'Удалённо', 'Офис', 'Медиана возраста', 'Гостов']
    rows = [[r['company'], r['total'], r['remote'], r['office'],
             _fmt(r['median_age']), r['ghosts']] for r in comp]
    w.table(cols, rows, '11. Топ работодателей (вакансий + удалёнка + медиана дней «висит»)')
    w.csv('11_companies.csv', cols, rows)


def report_company_sizes(a: Analyzer, w: ReportWriter) -> None:
    # 11c. ВСЕ работодатели по размеру: длинный хвост, который не виден в топ-30.
    sizes = a.companies_by_size()
    if not any(r['companies'] for r in sizes):
        return
    cols = ['Вакансий у компании', 'Компаний', 'Вакансий', 'Свежих', '30-60дн', 'Гостов', '%гостов']
    rows = [[r['bucket'], r['companies'], r['vacancies'], r['fresh'], r['recent'],
             r['ghost'], r['ghost_pct']] for r in sizes]
    w.table(cols, rows, '11c. Работодатели по числу вакансий (весь рынок + свежесть)')
    w.csv('11c_company_sizes.csv', cols, rows)


def report_by_source(a: Analyzer, w: ReportWriter) -> None:
    # 12. Разрез по порталам-источникам (агрегатор) — только если порталов >1
    src = a.by_source()
    if len(src) <= 1:
        return
    cols = ['Портал', 'Вакансий', 'Удалённо', 'Офис', 'Медиана возраста', 'Гостов']
    rows = [[r['source'], r['total'], r['remote'], r['office'],
             _fmt(r['median_age']), r['ghosts']] for r in src]
    w.table(cols, rows, '12. По порталам (охват источников агрегатора)')
    w.csv('12_sources.csv', cols, rows)


# Реестр отчётов = порядок генерации. Добавить отчёт = дописать функцию сюда.
_REPORTS = [
    report_cities, report_tech_by_city, report_salary_by_lang, report_salary_city_lang,
    report_top_stacks, report_salary_by_exp, report_lang_stacks, report_remote_by_city,
    report_freshness, report_companies, report_company_sizes, report_by_source,
]


def run_reports(vacs: list[Vacancy], out_dir: Path | None = None) -> None:
    """Сгенерировать отчёты. out_dir=None -> REPORTS_DIR (по умолчанию); иначе —
    по-портальный подкаталог (для фильтра источника в дашборде)."""
    a = Analyzer.with_live_rates(vacs)           # View-слой тянет FX; конструктор в сеть не ходит
    w = ReportWriter(out_dir or REPORTS_DIR)
    for report in _REPORTS:
        report(a, w)
    w.cleanup()                                 # чистим по факту записанного (не по хардкод-списку)
    log.success('Отчёты: {}', w.out_dir)
