"""Дашборд со-встречаемости навыков: дропдаун {{base}} -> что ищут ВМЕСТЕ с навыком
(было: cooccurrence_dashboard). Self-join core.vacancy_skills."""
from __future__ import annotations

from ..client import MetabaseClient
from ..config import PG_ENGINE, PG_NAME
from .base import bar, layout, text_tag

TITLE = "HH — стек рядом с языком (co-occurrence)"
P_BASE = "p_base"

CO_SQL = (
    "SELECT s2.name AS skill, count(*) AS vacancies "
    "FROM core.vacancy_skills v1 "
    "JOIN core.skills s1 ON s1.id = v1.skill_id AND s1.name = {{base}} "
    "JOIN core.vacancy_skills v2 ON v2.vacancy_id = v1.vacancy_id "
    "JOIN core.skills s2 ON s2.id = v2.skill_id "
    "WHERE s2.name <> {{base}} "
    "GROUP BY s2.name ORDER BY vacancies DESC LIMIT 36"
)
TOTAL_SQL = (
    "SELECT count(DISTINCT v1.vacancy_id) AS vacancies "
    "FROM core.vacancy_skills v1 JOIN core.skills s1 ON s1.id = v1.skill_id AND s1.name = {{base}}"
)


class CooccurrenceDashboard:
    key = "cooccurrence"
    title = TITLE

    def build(self, client: MetabaseClient) -> None:
        pg = client.find_database(PG_NAME, PG_ENGINE)

        def tags():  # свежие template-tags (новый uuid) на каждую карточку
            return text_tag("base", "Навык", required=True, default="Python")

        c_total = client.create_card("Всего вакансий с навыком", pg, TOTAL_SQL, "scalar", tags=tags())
        c_co = client.create_card("Что ищут ВМЕСТЕ с выбранным навыком", pg, CO_SQL,
                                  "row", bar("skill", "vacancies"), tags=tags())

        skills = [r[0] for r in client.run_sql(
            pg, "SELECT s.name FROM core.vacancy_skills vs JOIN core.skills s ON s.id=vs.skill_id "
                "GROUP BY s.name ORDER BY count(*) DESC")]
        params = [{
            "id": P_BASE, "name": "Базовый навык", "slug": "base", "type": "string/=",
            "sectionId": "string", "default": "Python",
            "values_source_type": "static-list", "values_source_config": {"values": skills},
        }]

        def pm(cid):
            return [{"parameter_id": P_BASE, "card_id": cid, "target": ["variable", ["template-tag", "base"]]}]

        d_id = client.upsert_dashboard(TITLE)
        items = [(c_total, 0, 0, 6, 3, pm(c_total)), (c_co, 3, 0, 24, 16, pm(c_co))]
        client.set_dashboard(d_id, layout(items), params)
        print(f"  [cooccurrence] -> /dashboard/{d_id} (дропдаун навыка + 36 со-встречаемостей)")
