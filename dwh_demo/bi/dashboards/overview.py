"""Обзорный дашборд по Postgres с фильтрами Город/Опыт (было: setup_metabase + add_filters)."""
from __future__ import annotations

from typing import Any

from etl.domain import EXPERIENCE, NO_EXPERIENCE_LABEL

from ..client import MetabaseClient
from ..config import PG_ENGINE, PG_NAME
from .base import REMOTE_LIKE, RUB, bar, layout, text_tag

TITLE = "HH — рынок труда (Python / Data Engineer)"
P_CITY, P_EXP = "p_city", "p_exp"
# Предикат опыта нормализует колонку ТАК ЖЕ, как витрина: у 19 % вакансий опыт не указан,
# и `v.experience = {{experience}}` до них не добирается — NULL не равен ничему.
F = ("[[ AND c.name = {{city}} ]] "
     "[[ AND coalesce(nullif(v.experience, ''), '" + NO_EXPERIENCE_LABEL + "') = {{experience}} ]]")
# Единый источник меток опыта — справочник домена (не дублируем, чтобы не было дрейфа),
# плюс бакет «не указан»: без него фильтр не достаёт пятую часть выборки (аудит 09.08.2026).
EXP_VALUES = [*EXPERIENCE.values(), NO_EXPERIENCE_LABEL]


def _tags() -> dict[str, Any]:
    # Значения — тела template-tag'ов Metabase (разнородный JSON), это внешняя граница.
    t = text_tag("city", "Город")
    t.update(text_tag("experience", "Опыт"))
    return t


class OverviewDashboard:
    key = "overview"
    title = TITLE

    def build(self, client: MetabaseClient) -> None:
        pg = client.find_database(PG_NAME, PG_ENGINE)

        def card(name: str, sql: str, display: str, viz: dict[str, Any] | None = None) -> int:
            return client.create_card(name, pg, sql, display, viz, tags=_tags())

        c_total = card("Всего вакансий",
                       f"SELECT count(*) FROM core.vacancies v "
                       f"LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F}", "scalar")
        c_remote = card(REMOTE_LIKE,
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
        # Обе средние — по ОДНОМУ множеству строк (известны обе рублёвые границы), иначе
        # серии «нижняя» и «верхняя» считались бы по разным выборкам. До 09.08.2026 карточка
        # усредняла СЫРЫЕ суммы прямо из факта, складывая 45 валют в одно число.
        _RUB_ROWS = "v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL"
        c_exp = card(f"Зарплата по опыту{RUB}",
                     f"SELECT coalesce(nullif(v.experience,''),'{NO_EXPERIENCE_LABEL}') AS experience, "
                     f"round(avg(v.salary_min_rub) FILTER (WHERE {_RUB_ROWS})) AS avg_salary_min_rub, "
                     f"round(avg(v.salary_max_rub) FILTER (WHERE {_RUB_ROWS})) AS avg_salary_max_rub "
                     f"FROM core.vacancies v LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F} "
                     f"GROUP BY coalesce(nullif(v.experience,''),'{NO_EXPERIENCE_LABEL}') "
                     f"ORDER BY avg_salary_max_rub",
                     "bar", bar("experience", "avg_salary_min_rub", "avg_salary_max_rub"))
        c_emp = card("Топ-15 работодателей",
                     f"SELECT e.name AS employer, count(*) AS vacancies FROM core.vacancies v "
                     f"JOIN core.employers e ON e.id=v.employer_id "
                     f"LEFT JOIN core.cities c ON c.id=v.city_id WHERE 1=1 {F} "
                     f"GROUP BY e.name ORDER BY vacancies DESC LIMIT 15", "row", bar("employer", "vacancies"))
        c_city = card(f"Города (топ-15): вакансии, з/п, «{REMOTE_LIKE}»{RUB}",
                      f"SELECT c.name AS city, count(*) AS vacancies, "
                      f"count(*) FILTER (WHERE {_RUB_ROWS}) AS with_salary_rub, "
                      f"round(avg(v.salary_max_rub) FILTER (WHERE {_RUB_ROWS})) AS avg_salary_max_rub, "
                      f"round(100.0*count(*) FILTER (WHERE v.is_remote)/count(*),1) AS remote_like_share_pct "
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

        def pm(cid: int) -> list[dict[str, Any]]:
            # Значение "target" — вложенный JSON Metabase (`["variable", ["template-tag", …]]`),
            # разнородный по определению, поэтому Any.
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
