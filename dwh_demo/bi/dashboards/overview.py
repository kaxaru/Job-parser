"""Обзорный дашборд по Postgres с фильтрами Город/Опыт (было: setup_metabase + add_filters)."""
from __future__ import annotations

from etl.domain import EXPERIENCE

from ..client import MetabaseClient
from ..config import PG_ENGINE, PG_NAME
from .base import bar, layout, text_tag

TITLE = "HH — рынок труда (Python / Data Engineer)"
P_CITY, P_EXP = "p_city", "p_exp"
F = "[[ AND c.name = {{city}} ]] [[ AND v.experience = {{experience}} ]]"
# единый источник меток опыта — справочник домена (не дублируем, чтобы не было дрейфа)
EXP_VALUES = list(EXPERIENCE.values())


def _tags():
    t = text_tag("city", "Город")
    t.update(text_tag("experience", "Опыт"))
    return t


class OverviewDashboard:
    key = "overview"
    title = TITLE

    def build(self, client: MetabaseClient) -> None:
        pg = client.find_database(PG_NAME, PG_ENGINE)

        def card(name, sql, display, viz=None):
            return client.create_card(name, pg, sql, display, viz, tags=_tags())

        c_total = card("Всего вакансий",
                       f"SELECT count(*) FROM core.vacancies v "
                       f"LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F}", "scalar")
        c_remote = card("Удалённые вакансии",
                        f"SELECT count(*) FROM core.vacancies v "
                        f"LEFT JOIN core.cities c ON c.id=v.city_id WHERE v.is_remote {F}", "scalar")
        c_salary = card("С указанной зарплатой",
                        f"SELECT count(*) FROM core.vacancies v "
                        f"LEFT JOIN core.cities c ON c.id=v.city_id "
                        f"WHERE (v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) {F}", "scalar")
        c_skills = card("Спрос на навыки (топ-15)",
                        f"SELECT s.name AS skill, count(*) AS vacancies FROM core.vacancies v "
                        f"JOIN core.vacancy_skills vs ON vs.vacancy_id=v.id "
                        f"JOIN core.skills s ON s.id=vs.skill_id "
                        f"LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F} "
                        f"GROUP BY s.name ORDER BY vacancies DESC LIMIT 15", "bar", bar("skill", "vacancies"))
        c_exp = card("Зарплата по опыту",
                     f"SELECT coalesce(v.experience,'не указан') AS experience, "
                     f"round(avg(v.salary_min)) AS avg_salary_min, round(avg(v.salary_max)) AS avg_salary_max "
                     f"FROM core.vacancies v LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F} "
                     f"GROUP BY coalesce(v.experience,'не указан') ORDER BY avg_salary_max",
                     "bar", bar("experience", "avg_salary_min", "avg_salary_max"))
        c_emp = card("Топ-15 работодателей",
                     f"SELECT e.name AS employer, count(*) AS vacancies FROM core.vacancies v "
                     f"JOIN core.employers e ON e.id=v.employer_id "
                     f"LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F} "
                     f"GROUP BY e.name ORDER BY vacancies DESC LIMIT 15", "row", bar("employer", "vacancies"))
        c_city = card("Города: вакансии, зарплаты, удалёнка (топ-15)",
                      f"SELECT c.name AS city, count(*) AS vacancies, "
                      f"count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary, "
                      f"round(avg(v.salary_max)) AS avg_salary_max, "
                      f"round(100.0*count(*) FILTER (WHERE v.is_remote)/count(*),1) AS remote_share_pct "
                      f"FROM core.vacancies v JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F} "
                      f"GROUP BY c.name ORDER BY vacancies DESC LIMIT 15", "table")

        cities = [r[0] for r in client.run_sql(
            pg, "SELECT c.name FROM core.vacancies v JOIN core.cities c ON c.id=v.city_id "
                "GROUP BY c.name ORDER BY count(*) DESC LIMIT 40")]
        params = [
            {"id": P_CITY, "name": "Город", "slug": "city", "type": "string/=", "sectionId": "string",
             "values_source_type": "static-list", "values_source_config": {"values": cities}},
            {"id": P_EXP, "name": "Опыт", "slug": "experience", "type": "string/=", "sectionId": "string",
             "values_source_type": "static-list", "values_source_config": {"values": EXP_VALUES}},
        ]

        def pm(cid):
            return [
                {"parameter_id": P_CITY, "card_id": cid, "target": ["variable", ["template-tag", "city"]]},
                {"parameter_id": P_EXP, "card_id": cid, "target": ["variable", ["template-tag", "experience"]]},
            ]

        d_id = client.upsert_dashboard(TITLE)
        items = [
            (c_total, 0, 0, 8, 3, pm(c_total)), (c_remote, 0, 8, 8, 3, pm(c_remote)),
            (c_salary, 0, 16, 8, 3, pm(c_salary)),
            (c_skills, 3, 0, 24, 7, pm(c_skills)),
            (c_exp, 10, 0, 12, 6, pm(c_exp)), (c_emp, 10, 12, 12, 6, pm(c_emp)),
            (c_city, 16, 0, 24, 7, pm(c_city)),
        ]
        client.set_dashboard(d_id, layout(items), params)
        print(f"  [overview] -> /dashboard/{d_id} (7 карточек, фильтры Город/Опыт)")
