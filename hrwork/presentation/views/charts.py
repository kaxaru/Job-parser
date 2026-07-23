"""Plotly-графики дашборда (строятся из CSV-отчётов)."""
import csv

import plotly.graph_objects as go

from hrwork.config import ACCENT, BG, GRID, PAPER, REPORTS_DIR, TEXT
from hrwork.domain.freshness import FreshnessClass

# Каталог CSV-отчётов передаётся ЯВНО в каждую chart_*-функцию (по-портальные срезы дашборда
# читают свой подкаталог). Раньше был module-global _BASE + set_reports_dir — mutate/restore
# в dashboard и гонка при конкурентной сборке; параметр убирает скрытое состояние.


def _read(base, name: str) -> list[dict]:
    path = base / name
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [r for r in reader if any(v.strip() for v in r.values())]


def _num(s: str) -> float | None:
    s = s.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not s or s == "—":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _layout(title: str, **kw) -> dict:
    return dict(
        title=dict(text=title, font=dict(size=18, color=TEXT), x=0.5),
        paper_bgcolor=PAPER,
        plot_bgcolor=BG,
        font=dict(color=TEXT, family="Arial, sans-serif"),
        margin=dict(l=80, r=30, t=60, b=80),
        **kw,
    )


def _stacked_pct_bars(title: str, ylabels: list[str], series, *,
                      noun: str, xaxis_title: str, yaxis_title: str = "") -> go.Figure:
    """Горизонтальный стек, нормированный к 100% (barnorm) — единый вид «состав по долям»
    для companies_sizes / freshness / remote (раньше блок дублировался 3×: _pcts, kwargs
    трейсов, layout — правка стиля требовала трёх синхронных правок).

    series: [(имя, значения, цвет)]; noun — существительное для ховера («компаний»/«вакансий»).
    Проценты на всех сегментах; база доли = сумма ПЕРЕДАННЫХ рядов (unknown не передаём —
    как нормирует barnorm); зазор между сегментами — цветом подложки."""
    totals = [sum(vals[i] for _n, vals, _c in series) for i in range(len(ylabels))]
    fig = go.Figure()
    for name, vals, color in series:
        pcts = [round(v * 100 / t) if t else 0 for v, t in zip(vals, totals)]
        fig.add_trace(go.Bar(
            name=name, x=vals, y=ylabels, orientation="h",
            marker=dict(color=color, line=dict(color=BG, width=2)),
            # Plotly сам прячет подпись, если не влезла в узкий сегмент (crop не делаем)
            text=[f"{p}%" for p in pcts],
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color=TEXT), insidetextfont=dict(color=TEXT),
            cliponaxis=False,
            hovertemplate="%{y}<br>" + name + ": %{x} " + noun + "<extra></extra>",
        ))
    fig.update_layout(
        **_layout(title),
        barmode="stack",
        barnorm="percent",                           # каждый бар -> 100%, сравниваем состав
        height=max(520, len(ylabels) * 34 + 100),
        xaxis=dict(title=xaxis_title, showgrid=True, gridcolor=GRID,
                   ticksuffix="%", range=[0, 100]),
        yaxis=dict(title=yaxis_title, showgrid=False, tickfont=dict(size=13)),
        legend=dict(orientation="h", y=-0.1, yanchor="top", x=0.5, xanchor="center"),
    )
    return fig


def chart_cities(base=REPORTS_DIR) -> go.Figure:
    # Топ-30 по числу вакансий (как chart_remote/companies). Без кэпа рисовались ВСЕ ~3000
    # городов (полотно ~100k px), а мегастроки-«города» talanto (списки стран до 2400 симв.)
    # разносили левое поле. Ярлык дополнительно усечён — на случай длинного имени в топе.
    rows = sorted(_read(base, "01_cities.csv"), key=lambda r: int(r["Вакансий"]), reverse=True)[:30]
    cities = [(r["Город"] or "")[:40] for r in rows]
    vals = [int(r["Вакансий"]) for r in rows]
    cities_r = list(reversed(cities))
    vals_r = list(reversed(vals))

    fig = go.Figure(go.Bar(
        x=vals_r, y=cities_r, orientation="h",
        marker=dict(color=vals_r, colorscale="Blues", showscale=False),
        text=vals_r, textposition="outside",
    ))
    height = max(520, len(cities) * 34 + 100)
    fig.update_layout(**_layout("Вакансий по городам"),
                      height=height,
                      xaxis=dict(showgrid=True, gridcolor=GRID),
                      yaxis=dict(showgrid=False, tickfont=dict(size=13)))
    return fig


def chart_salary_lang(base=REPORTS_DIR) -> go.Figure:
    rows = _read(base, "03_salary_by_lang.csv")
    langs = [r["Язык"] for r in rows]
    medians = [_num(r["Медиана"]) for r in rows]
    p25 = [_num(r["P25"]) for r in rows]
    p75 = [_num(r["P75"]) for r in rows]

    err_minus = [m - lo if m and lo else 0 for m, lo in zip(medians, p25)]
    err_plus  = [hi - m if m and hi else 0 for m, hi in zip(medians, p75)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=langs, y=medians,
        name="Медиана",
        marker_color=ACCENT,
        error_y=dict(type="data", symmetric=False,
                     array=err_plus, arrayminus=err_minus,
                     color="#EECA3B", thickness=2, width=6),
        text=[f"{int(v):,}" if v else "" for v in medians],
        textposition="outside",
    ))
    fig.update_layout(
        **_layout("Зарплата по языкам, net RUB (медиана + IQR)"),
        xaxis=dict(showgrid=False),
        yaxis=dict(title="RUB / месяц", showgrid=True, gridcolor=GRID),
        bargap=0.35,
    )
    return fig


def chart_salary_exp(base=REPORTS_DIR) -> go.Figure:
    rows = _read(base, "06_salary_by_exp.csv")
    labels = [r["Опыт"] for r in rows]
    medians = [_num(r["Медиана"]) for r in rows]
    p25 = [_num(r["P25"]) for r in rows]
    p75 = [_num(r["P75"]) for r in rows]
    ns = [int(r["N"]) for r in rows]

    colors = ["#4C78A8", "#54A24B", "#F58518", "#E45756"]

    fig = go.Figure()
    for i, (lbl, med, lo, hi, n) in enumerate(zip(labels, medians, p25, p75, ns)):
        if med is None:
            continue
        fig.add_trace(go.Bar(
            name=lbl,
            x=[lbl], y=[med],
            marker_color=colors[i % len(colors)],
            error_y=dict(type="data", symmetric=False,
                         array=[hi - med if hi else 0],
                         arrayminus=[med - lo if lo else 0],
                         color="#EECA3B", thickness=2, width=10),
            text=f"N={n}<br>Медиана {int(med):,}",
            textposition="outside",
        ))
    fig.update_layout(
        **_layout("Зарплата по уровню опыта, net RUB"),
        showlegend=False,
        xaxis=dict(showgrid=False),
        yaxis=dict(title="RUB / месяц", showgrid=True, gridcolor=GRID),
        bargap=0.4,
    )
    return fig


def chart_stacks(base=REPORTS_DIR) -> go.Figure:
    rows = _read(base, "05_top_stacks.csv")
    stacks = [r["Стек"] for r in rows]
    counts = [int(r["Упоминаний"]) for r in rows]
    stacks_r = list(reversed(stacks))
    counts_r = list(reversed(counts))

    fig = go.Figure(go.Bar(
        x=counts_r, y=stacks_r, orientation="h",
        marker=dict(color=counts_r, colorscale="Viridis", showscale=False),
        text=counts_r, textposition="outside",
    ))
    fig.update_layout(
        **_layout("Топ-20 пар языков (совместные упоминания)"),
        xaxis=dict(showgrid=True, gridcolor=GRID),
        yaxis=dict(showgrid=False),
        height=600,
    )
    return fig


def chart_heatmap_city_lang(base=REPORTS_DIR) -> go.Figure:
    rows = _read(base, "04_salary_city_lang.csv")
    # Топ-30 городов по числу язык-ячеек (без кэпа все ~сотни городов растягивали карту).
    cnt: dict[str, int] = {}
    for r in rows:
        cnt[r["Город"]] = cnt.get(r["Город"], 0) + 1
    top_cities = {c for c, _ in sorted(cnt.items(), key=lambda x: -x[1])[:30]}
    rows = [r for r in rows if r["Город"] in top_cities]
    cities_ord = sorted(top_cities)                                 # алфавит среди топ-городов
    langs_ord  = sorted({r["Язык"]  for r in rows})

    mat = {c: dict.fromkeys(langs_ord) for c in cities_ord}
    for r in rows:
        v = _num(r["Медиана"])
        if v:
            mat[r["Город"]][r["Язык"]] = v

    z = [[mat[c][lang] for lang in langs_ord] for c in cities_ord]
    text = [[f"{int(v):,}" if v else "" for v in row] for row in z]

    fig = go.Figure(go.Heatmap(
        z=z, x=langs_ord, y=[c[:40] for c in cities_ord],
        text=text, texttemplate="%{text}",
        colorscale="Blues",
        colorbar=dict(title="RUB", tickfont=dict(color=TEXT)),
        zmin=50_000, zmax=300_000,
    ))
    height = max(520, len(cities_ord) * 28 + 120)
    fig.update_layout(
        **_layout("Медианная зарплата (net RUB): город x язык"),
        xaxis=dict(showgrid=False, tickangle=-30),
        yaxis=dict(showgrid=False, tickfont=dict(size=11)),
        height=height,
    )
    return fig


def chart_lang_stack(base, lang: str, csv_name: str, color: str) -> go.Figure:
    rows = _read(base, csv_name)
    techs  = [r["Технология"] for r in rows]
    counts = [int(r["Вакансий"]) for r in rows]
    techs_r  = list(reversed(techs))
    counts_r = list(reversed(counts))

    fig = go.Figure(go.Bar(
        x=counts_r, y=techs_r, orientation="h",
        marker=dict(color=counts_r, colorscale=color, showscale=False),
        text=counts_r, textposition="outside",
    ))
    height = max(520, len(techs) * 30 + 100)
    fig.update_layout(
        **_layout(f"Стек для {lang} — топ-25 сопутствующих технологий"),
        height=height,
        xaxis=dict(showgrid=True, gridcolor=GRID),
        yaxis=dict(showgrid=False, tickfont=dict(size=13)),
    )
    return fig


def chart_python_stack(base=REPORTS_DIR) -> go.Figure:
    return chart_lang_stack(base, "Python", "07_python_stack.csv", "Blues")


def chart_js_stack(base=REPORTS_DIR) -> go.Figure:
    return chart_lang_stack(base, "JavaScript", "08_js_stack.csv", "YlOrRd")


def chart_remote(base=REPORTS_DIR) -> go.Figure:
    """Удалёнка по городам: 100%-стек удалёнка/офис. Топ-25 по числу вакансий (Москва сверху)."""
    rows = _read(base, "09_remote_by_city.csv")
    # asc -> в plotly последний сверху, т.е. крупнейший город первым
    rows_s = sorted(rows, key=lambda r: int(r["Всего"]))[-25:]
    return _stacked_pct_bars(
        "Удалёнка по городам: доля удалёнка/офис (%)",
        [r["Город"] for r in rows_s],
        [("Удалённо", [int(r["Удалённо"]) for r in rows_s], "#54A24B"),
         ("Офис",     [int(r["Офис"]) for r in rows_s],     "#4C78A8")],
        noun="вакансий", xaxis_title="Доля вакансий, %",
    )


def chart_companies(base=REPORTS_DIR) -> go.Figure:
    """Топ работодателей: длина бара = число вакансий, цвет = медиана «сколько висят»
    (температура: свежие — горячие/красные, старые — холодные/синие)."""
    rows = _read(base, "11_companies.csv")[:25]
    rows_r = list(reversed(rows))                       # топ сверху на гориз. барах
    comps  = [r["Компания"] for r in rows_r]
    totals = [int(r["Вакансий"]) for r in rows_r]
    ages   = [_num(r["Медиана возраста"]) or 0 for r in rows_r]
    labels = [f'{t}  ·  {int(a)}д' if a else str(t) for t, a in zip(totals, ages)]

    fig = go.Figure(go.Bar(
        x=totals, y=comps, orientation="h",
        marker=dict(
            color=ages, colorscale="RdYlBu",            # low age=красный(горячо), high=синий(холодно)
            cmin=0, cmax=90,
            colorbar=dict(title="Медиана,<br>дней", tickfont=dict(color=TEXT)),
        ),
        text=labels, textposition="outside",
    ))
    height = max(520, len(comps) * 30 + 120)
    fig.update_layout(
        **_layout("Работодатели: вакансий (бар) × медиана «сколько висят» (цвет)"),
        height=height,
        xaxis=dict(title="Вакансий", showgrid=True, gridcolor=GRID),
        yaxis=dict(showgrid=False, tickfont=dict(size=12)),
    )
    return fig


def chart_companies_sizes(base=REPORTS_DIR) -> go.Figure:
    """ВСЕ работодатели по числу вакансий: бары нормированы к 100%, стек = доля
    компаний в каждом классе свежести (по медиане возраста вакансий компании).

    Абсолютные счётчики корзин отличаются в тысячу раз (одиночек 5252, крупных единицы),
    поэтому на общей оси мелкие корзины были невидимы. Нормировка к 100% уравнивает
    длину баров и делает читаемым то, ради чего график и нужен — СОСТАВ по свежести.
    Число компаний остаётся в подписи корзины и в ховере.
    """
    rows = _read(base, "11c_company_sizes.csv")
    rows_r = list(reversed(rows))                    # «1» сверху -> «100+» снизу
    buckets = [f'{r["Вакансий у компании"]}  ({r["Компаний"]})' for r in rows_r]
    # порядок стека = порядок шкалы свежести; unknown не рисуем (база доли = датированные)
    series = [(fc.label, [int(r[col]) for r in rows_r], fc.color)
              for fc, col in ((FreshnessClass.FRESH, "Свежих"),
                              (FreshnessClass.RECENT, "30-60дн"),
                              (FreshnessClass.GHOST, "Гостов"))]
    return _stacked_pct_bars(
        "Все работодатели: размер (вакансий у компании) × свежесть",
        buckets, series,
        noun="компаний", xaxis_title="Доля компаний, %", yaxis_title="Вакансий у компании",
    )


def chart_by_source(base=REPORTS_DIR) -> go.Figure:
    """Разрез по порталам-источникам агрегатора: число вакансий (бар) + доля удалёнки."""
    rows   = _read(base, "12_sources.csv")
    srcs   = [r["Портал"] for r in rows]
    totals = [int(r["Вакансий"]) for r in rows]
    remote = [int(r["Удалённо"]) for r in rows]
    labels = [f'{t}  ·  {round(rm * 100 / t) if t else 0}% удал.'
              for t, rm in zip(totals, remote)]
    fig = go.Figure(go.Bar(
        x=srcs, y=totals, marker_color="#4C78A8",
        text=labels, textposition="outside",
    ))
    fig.update_layout(
        **_layout("Порталы: вакансий по источникам агрегатора"),
        xaxis=dict(title="Портал", tickfont=dict(color=TEXT, size=13)),
        yaxis=dict(title="Вакансий", showgrid=True, gridcolor=GRID),
    )
    return fig


def chart_companies_remote(base=REPORTS_DIR) -> go.Figure:
    """Топ работодателей: удалёнка vs офис (стек). Кто реально берёт на удалёнку."""
    rows = _read(base, "11_companies.csv")[:25]
    rows_s = sorted(rows, key=lambda r: int(r["Вакансий"]))   # больше вакансий — выше
    comps  = [r["Компания"] for r in rows_s]
    remote = [int(r.get("Удалённо", 0) or 0) for r in rows_s]
    office = [int(r.get("Офис", 0) or 0) for r in rows_s]
    total  = [rm + of for rm, of in zip(remote, office)]
    pcts   = [round(rm * 100 / t) if t else 0 for rm, t in zip(remote, total)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Удалённо", x=remote, y=comps, orientation="h",
        marker_color="#54A24B",
        text=[f"{p}%" for p in pcts], textposition="inside",
    ))
    fig.add_trace(go.Bar(
        name="Офис", x=office, y=comps, orientation="h", marker_color="#4C78A8",
    ))
    height = max(520, len(comps) * 30 + 120)
    fig.update_layout(
        **_layout("Удалёнка по работодателям (топ по числу вакансий)"),
        barmode="stack",
        height=height,
        xaxis=dict(title="Вакансий", showgrid=True, gridcolor=GRID),
        yaxis=dict(showgrid=False, tickfont=dict(size=12)),
        legend=dict(orientation="h", y=-0.06, yanchor="top", x=0.5, xanchor="center"),
    )
    return fig


def chart_freshness(base=REPORTS_DIR) -> go.Figure:
    """Свежесть по городам: 100%-стек по классам свежести. Топ-25 по числу вакансий."""
    rows = _read(base, "10_freshness_by_city.csv")
    # asc -> в plotly последний сверху, т.е. крупнейший город (Москва) первым
    rows_s = sorted(rows, key=lambda r: int(r["Всего"]))[-25:]
    series = [(fc.label, [int(r.get(col, 0) or 0) for r in rows_s], fc.color)
              for fc, col in ((FreshnessClass.FRESH, "Свежих"),
                              (FreshnessClass.RECENT, "30-60дн"),
                              (FreshnessClass.GHOST, "Гостов"))]
    return _stacked_pct_bars(
        "Свежесть по городам: состав по классам (доля, %)",
        [r["Город"] for r in rows_s], series,
        noun="вакансий", xaxis_title="Доля вакансий, %",
    )


def chart_tech_heatmap(base=REPORTS_DIR, top_n: int = 40) -> go.Figure:
    """Топ-N технологий по всем городам — тепловая карта количеств."""
    rows = _read(base, "02_tech_by_city.csv")

    totals: dict[str, int] = {}
    for r in rows:
        t, n = r["Технология"], int(r["Упоминаний"])
        totals[t] = totals.get(t, 0) + n
    top = [t for t, _ in sorted(totals.items(), key=lambda x: -x[1])[:top_n]]

    # Топ-30 городов по суммарным упоминаниям (без кэпа — все ~3000 городов + мегастроки
    # talanto разносили тепловую карту по высоте и левому полю). Ярлык дополнительно усечён.
    city_totals: dict[str, int] = {}
    for r in rows:
        city_totals[r["Город"]] = city_totals.get(r["Город"], 0) + int(r["Упоминаний"])
    top_cities = {c for c, _ in sorted(city_totals.items(), key=lambda x: -x[1])[:30]}
    cities_ord = sorted(top_cities)                                 # алфавит среди топ-городов

    mat = {c: dict.fromkeys(top, 0) for c in cities_ord}
    for r in rows:
        if r["Технология"] in top and r["Город"] in top_cities:
            mat[r["Город"]][r["Технология"]] = int(r["Упоминаний"])

    z = [[mat[c][t] for t in top] for c in cities_ord]
    text = [[str(v) if v else "" for v in row] for row in z]

    fig = go.Figure(go.Heatmap(
        z=z, x=top, y=[c[:40] for c in cities_ord],
        text=text, texttemplate="%{text}",
        colorscale="YlOrRd",
        colorbar=dict(title="Вакансий", tickfont=dict(color=TEXT)),
    ))
    height = max(520, len(cities_ord) * 28 + 120)
    fig.update_layout(
        **_layout(f"Топ-{len(top)} технологий по топ-{len(cities_ord)} городам (кол-во вакансий)"),
        xaxis=dict(showgrid=False, tickangle=-30),
        yaxis=dict(showgrid=False, tickfont=dict(size=11)),
        height=height,
    )
    return fig


# латентность отказа: горячее = быстрее (вероятный ATS-автобан), холодное = дольше (человек смотрел)
_FUNNEL_BUCKETS = [
    ("<=10 мин",  "#B22222"),   # мгновенный автобан
    ("10-60 мин", "#E45756"),
    ("1 ч-1 день", "#F58518"),
    (">1 дня",    "#4C78A8"),   # медленно = скорее человек
    ("без метки", "#8C8C8C"),   # отказ по статусу, чат-сообщения нет -> латентность неизвестна
]


def chart_company_funnel(base=REPORTS_DIR, top_n: int = 18) -> go.Figure:
    """Латентность автоотказа по компаниям: горизонтальный стек по бакетам времени
    «отклик -> отказ». Быстрые (горячие) сегменты = вероятный ATS-автобан, где резюме
    не читали. Работодатель неизвестен («вне выдачи») в бары не идёт — не actionable.

    Гипотеза родительской задачи: доля <=1ч показывает, кто банит фильтром, а не человеком.
    """
    rows = _read(base, "13_company_funnel.csv")

    # сводка по ВСЕМ строкам (включая «вне выдачи») — для подписи; бары — только по known
    tot_rej = sum(int(r["Отказов"]) for r in rows)
    tot_1h  = sum(int(r["<=1ч"]) for r in rows)
    known = [r for r in rows if r["Компания"] != "(вне выдачи)" and int(r["Отказов"]) > 0]
    # порядок строк CSV = ранжирование funnel.compute_funnel (единое для таблицы и графика);
    # горизонтальные бары рисуются снизу вверх -> разворачиваем, чтобы топ был сверху
    known = list(reversed(known[:top_n]))

    comps = [r["Компания"][:34] for r in known]
    seg = {name: [] for name, _c in _FUNNEL_BUCKETS}
    for r in known:
        rej, meas = int(r["Отказов"]), int(r["Измерено"])
        le10, le1h, le1d = int(r["<=10м"]), int(r["<=1ч"]), int(r["<=1д"])
        seg["<=10 мин"].append(le10)
        seg["10-60 мин"].append(le1h - le10)
        seg["1 ч-1 день"].append(le1d - le1h)
        seg[">1 дня"].append(meas - le1d)
        seg["без метки"].append(rej - meas)              # отказ по статусу без чат-сообщения

    fig = go.Figure()
    for name, color in _FUNNEL_BUCKETS:
        fig.add_trace(go.Bar(
            name=name, x=seg[name], y=comps, orientation="h", marker_color=color,
            hovertemplate="%{y}<br>" + name + ": %{x} отказов<extra></extra>",
        ))
    height = max(520, len(comps) * 32 + 140)
    pct_1h = round(tot_1h * 100 / tot_rej) if tot_rej else 0
    fig.update_layout(
        **_layout(f"Латентность автоотказа по компаниям  "
                  f"(всего отказов {tot_rej}, из них <=1ч — {tot_1h} = {pct_1h}%)"),
        barmode="stack",
        height=height,
        xaxis=dict(title="Отказов", showgrid=True, gridcolor=GRID),
        yaxis=dict(showgrid=False, tickfont=dict(size=12)),
        legend=dict(orientation="h", y=-0.08, yanchor="top", x=0.5, xanchor="center"),
    )
    return fig
