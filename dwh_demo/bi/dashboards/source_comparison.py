"""Дашборд сравнения ПОРТАЛОВ-ИСТОЧНИКОВ (hh / hirify / talanto).

Не путать с ComparisonDashboard — тот сравнивает ДВИЖКИ (pg/clickhouse/mssql) на одних
данных. Здесь один движок (Postgres), но разрез по `source`: объёмы, зарплаты, доля
remote, топ-навыки. Читает витрину mart.source_stats (объёмы/зарплаты/remote) и core
(навыки в разрезе источника). Появился после подключения talanto (2026-07-22)."""
from __future__ import annotations

from ..client import MetabaseClient
from ..config import PG_ENGINE, PG_NAME
from .base import layout

TITLE = "Источники — hh vs hirify vs talanto"


def _bar2(dim: str, series: str, metric: str) -> dict:
    """Столбчатая с группировкой по второму измерению (dim × series -> metric)."""
    return {"graph.dimensions": [dim, series], "graph.metrics": [metric]}


class SourceComparisonDashboard:
    key = "sources"
    title = TITLE

    def build(self, client: MetabaseClient) -> None:
        pg = client.find_database(PG_NAME, PG_ENGINE)

        def card(name, sql, display, viz=None):
            return client.create_card(name, pg, sql, display, viz, tags={})

        c_vol = card("Вакансий по источникам",
                     "SELECT source, vacancies FROM mart.source_stats ORDER BY vacancies DESC",
                     "bar", {"graph.dimensions": ["source"], "graph.metrics": ["vacancies"]})
        c_sal = card("Медиана зарплатной вилки по источникам",
                     "SELECT source, median_salary_min, median_salary_max "
                     "FROM mart.source_stats ORDER BY median_salary_max DESC NULLS LAST",
                     "bar", {"graph.dimensions": ["source"],
                             "graph.metrics": ["median_salary_min", "median_salary_max"]})
        c_rem = card("Доля удалёнки по источникам, %",
                     "SELECT source, remote_share_pct FROM mart.source_stats "
                     "ORDER BY remote_share_pct DESC",
                     "bar", {"graph.dimensions": ["source"], "graph.metrics": ["remote_share_pct"]})
        c_cov = card("Покрытие зарплатой по источникам",
                     "SELECT source, vacancies, with_salary, "
                     "round(100.0*with_salary/vacancies,1) AS salary_coverage_pct "
                     "FROM mart.source_stats ORDER BY vacancies DESC", "table")
        c_skill = card("Топ-навыки в разрезе источника (топ-12)",
                       "SELECT s.name AS skill, v.source, count(*) AS vacancies "
                       "FROM core.vacancies v "
                       "JOIN core.vacancy_skills vs ON vs.vacancy_id=v.id "
                       "JOIN core.skills s ON s.id=vs.skill_id "
                       "WHERE s.name IN (SELECT s2.name FROM core.vacancy_skills vs2 "
                       "  JOIN core.skills s2 ON s2.id=vs2.skill_id "
                       "  GROUP BY s2.name ORDER BY count(*) DESC LIMIT 12) "
                       "GROUP BY s.name, v.source ORDER BY skill, v.source",
                       "bar", _bar2("skill", "source", "vacancies"))

        d_id = client.upsert_dashboard(TITLE)
        items = [
            (c_vol, 0, 0, 12, 6), (c_sal, 0, 12, 12, 6),
            (c_rem, 6, 0, 12, 6), (c_cov, 6, 12, 12, 6),
            (c_skill, 12, 0, 24, 8),
        ]
        client.set_dashboard(d_id, layout(items), [])
        print(f"  [sources] -> /dashboard/{d_id} (5 карточек: объём/зарплата/remote/навыки x источник)")
