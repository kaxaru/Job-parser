"""Сравнительный дашборд «один BI, два движка»: 7 метрик × Postgres/ClickHouse
(было: add_clickhouse_mb + extend_compare_dashboard)."""
from __future__ import annotations

from ..client import MetabaseClient
from ..config import CH_ENGINE, CH_NAME, PG_ENGINE, PG_NAME
from .base import REMOTE_LIKE, RUB, bar, layout

TITLE = "HH — Postgres vs ClickHouse (один BI, два движка)"
_SAL_VIZ = {"graph.dimensions": ["experience"],
            "graph.metrics": ["avg_salary_min_rub", "avg_salary_max_rub"]}


class ComparisonDashboard:
    key = "comparison"
    title = TITLE

    def build(self, client: MetabaseClient) -> None:
        pg = client.find_database(PG_NAME, PG_ENGINE)
        ch = client.find_database(CH_NAME, CH_ENGINE)
        emp = bar("employer", "vacancies")
        skl = bar("skill", "vacancies")

        # (имя, db, sql, display, viz)
        cards = {
            "pg_total": ("Всего вакансий · Postgres", pg, "SELECT count(*) AS vacancies FROM core.vacancies", "scalar", None),
            "ch_total": ("Всего вакансий · ClickHouse", ch, "SELECT count() AS vacancies FROM hh.vacancies", "scalar", None),
            "pg_remote": (f"{REMOTE_LIKE} · Postgres", pg, "SELECT count(*) AS vacancies FROM core.vacancies WHERE is_remote", "scalar", None),
            "ch_remote": (f"{REMOTE_LIKE} · ClickHouse", ch, "SELECT countIf(is_remote = 1) AS vacancies FROM hh.vacancies", "scalar", None),
            "pg_sal": ("С зарплатой · Postgres", pg, "SELECT count(*) AS vacancies FROM core.vacancies WHERE salary_min IS NOT NULL OR salary_max IS NOT NULL", "scalar", None),
            "ch_sal": ("С зарплатой · ClickHouse", ch, "SELECT countIf(isNotNull(salary_min) OR isNotNull(salary_max)) AS vacancies FROM hh.vacancies", "scalar", None),
            "pg_skills": ("Спрос на навыки · Postgres (mart REFRESH)", pg, "SELECT skill, vacancies FROM mart.skill_demand ORDER BY vacancies DESC LIMIT 15", "bar", skl),
            "ch_skills": ("Спрос на навыки · ClickHouse (AggregatingMergeTree)", ch, "SELECT skill, countMerge(vacancies) AS vacancies FROM hh.skill_demand GROUP BY skill ORDER BY vacancies DESC LIMIT 15", "bar", skl),
            "pg_exp": (f"Зарплата по опыту · Postgres{RUB}", pg, "SELECT experience, avg_salary_min_rub, avg_salary_max_rub FROM mart.salary_by_experience ORDER BY avg_salary_max_rub", "bar", _SAL_VIZ),
            "ch_exp": (f"Зарплата по опыту · ClickHouse (avgMerge){RUB}", ch, "SELECT exp_bucket AS experience, floor(avgMerge(avg_min_rub) + 0.5) AS avg_salary_min_rub, floor(avgMerge(avg_max_rub) + 0.5) AS avg_salary_max_rub FROM hh.salary_by_exp GROUP BY exp_bucket ORDER BY avg_salary_max_rub", "bar", _SAL_VIZ),
            "pg_emp": ("Топ-15 работодателей · Postgres", pg, "SELECT employer, vacancies FROM mart.top_employers ORDER BY vacancies DESC LIMIT 15", "row", emp),
            "ch_emp": ("Топ-15 работодателей · ClickHouse", ch, "SELECT employer, count() AS vacancies FROM hh.vacancies WHERE isNotNull(employer) GROUP BY employer ORDER BY vacancies DESC LIMIT 15", "row", emp),
            "pg_cit": (f"Города · Postgres{RUB}", pg, "SELECT city, vacancies, with_salary_rub, avg_salary_max_rub, remote_share_pct AS remote_like_share_pct FROM mart.city_stats ORDER BY vacancies DESC LIMIT 15", "table", None),
            "ch_cit": (f"Города · ClickHouse{RUB}", ch, "SELECT city, count() AS vacancies, countIf(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub)) AS with_salary_rub, floor(avgIf(salary_max_rub, isNotNull(salary_min_rub) AND isNotNull(salary_max_rub)) + 0.5) AS avg_salary_max_rub, round(100.0*countIf(is_remote=1)/count(),1) AS remote_like_share_pct FROM hh.vacancies WHERE isNotNull(city) GROUP BY city ORDER BY vacancies DESC LIMIT 15", "table", None),
        }
        ids = {k: client.create_card(n, db, sql, disp, viz) for k, (n, db, sql, disp, viz) in cards.items()}

        d_id = client.upsert_dashboard(TITLE)
        items = [
            (ids["pg_total"], 0, 0, 12, 3), (ids["ch_total"], 0, 12, 12, 3),
            (ids["pg_skills"], 3, 0, 12, 8), (ids["ch_skills"], 3, 12, 12, 8),
            (ids["pg_exp"], 11, 0, 12, 6), (ids["ch_exp"], 11, 12, 12, 6),
            (ids["pg_remote"], 17, 0, 6, 3), (ids["ch_remote"], 17, 6, 6, 3),
            (ids["pg_sal"], 17, 12, 6, 3), (ids["ch_sal"], 17, 18, 6, 3),
            (ids["pg_emp"], 20, 0, 12, 8), (ids["ch_emp"], 20, 12, 12, 8),
            (ids["pg_cit"], 28, 0, 12, 7), (ids["ch_cit"], 28, 12, 12, 7),
        ]
        client.set_dashboard(d_id, layout(items))
        print(f"  [comparison] -> /dashboard/{d_id} (14 карточек = 7 метрик x 2 движка)")
