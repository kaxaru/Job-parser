"""Обзорный дашборд по MS SQL (T-SQL): метрики рынка из звезды/витрин в hh-mssql.

Отдельный дашборд под Microsoft-стек (закрытие гэпа вакансии T-SQL + BI).
SQL — на T-SQL (TOP вместо LIMIT, COUNT(*) и т.п.).
"""
from __future__ import annotations

from ..client import MetabaseClient
from ..config import MS_ENGINE, MS_NAME
from .base import MIXED_CURRENCY, REMOTE_LIKE, bar, layout

TITLE = "HH — MS SQL (T-SQL DWH)"
_SAL_VIZ = {"graph.dimensions": ["experience"], "graph.metrics": ["avg_salary_min", "avg_salary_max"]}


class MssqlOverviewDashboard:
    key = "mssql"
    title = TITLE

    def build(self, client: MetabaseClient) -> None:
        db = client.find_database(MS_NAME, MS_ENGINE)

        def card(name, sql, display, viz=None):
            return client.create_card(name, db, sql, display, viz)

        c_total = card("Всего вакансий", "SELECT COUNT(*) AS vacancies FROM core.vacancies", "scalar")
        c_remote = card(REMOTE_LIKE,
                        "SELECT COUNT(*) AS vacancies FROM core.vacancies WHERE is_remote = 1", "scalar")
        c_salary = card("С зарплатой",
                        "SELECT COUNT(*) AS vacancies FROM core.vacancies "
                        "WHERE salary_min IS NOT NULL OR salary_max IS NOT NULL", "scalar")
        c_skills = card("Спрос на навыки (топ-15)",
                        "SELECT TOP 15 skill, vacancies FROM mart.skill_demand ORDER BY vacancies DESC",
                        "bar", bar("skill", "vacancies"))
        c_exp = card(f"Зарплата по опыту{MIXED_CURRENCY}",
                     "SELECT experience, avg_salary_min, avg_salary_max "
                     "FROM mart.salary_by_experience ORDER BY avg_salary_max",
                     "bar", _SAL_VIZ)
        c_emp = card("Топ-15 работодателей",
                     "SELECT TOP 15 employer, vacancies FROM mart.top_employers ORDER BY vacancies DESC",
                     "row", bar("employer", "vacancies"))
        c_city = card(f"Города (топ-15){MIXED_CURRENCY}",
                      "SELECT TOP 15 city, vacancies, with_salary, avg_salary_max, "
                      "remote_share_pct AS remote_like_share_pct "
                      "FROM mart.city_stats ORDER BY vacancies DESC", "table")

        d_id = client.upsert_dashboard(TITLE)
        items = [
            (c_total, 0, 0, 8, 3), (c_remote, 0, 8, 8, 3), (c_salary, 0, 16, 8, 3),
            (c_skills, 3, 0, 24, 7),
            (c_exp, 10, 0, 12, 6), (c_emp, 10, 12, 12, 6),
            (c_city, 16, 0, 24, 7),
        ]
        client.set_dashboard(d_id, layout(items))
        print(f"  [mssql] -> /dashboard/{d_id} (7 карточек, источник MS SQL / T-SQL)")
