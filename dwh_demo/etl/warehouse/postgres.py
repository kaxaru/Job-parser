"""Адаптер Postgres: типизированные записи -> staging.stg_* -> core (звезда) ->
REFRESH mart.* (материализованные витрины)."""
from __future__ import annotations

from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from ..domain import Vacancy

BATCH_SIZE = 1000   # строк в одном многострочном INSERT (баланс round-trip / память)

STG_COLUMNS = ["id", "source", "name", "city_name", "employer_name", "salary_min", "salary_max",
               "salary_currency", "salary_gross", "experience", "schedule", "is_remote",
               "url", "query"]

# справочники -> факт -> мост -> обновление витрин
LOAD_SQL = """
INSERT INTO core.cities(name)    SELECT DISTINCT city_name     FROM staging.stg_vacancies WHERE city_name     IS NOT NULL ON CONFLICT (name) DO NOTHING;
INSERT INTO core.employers(name) SELECT DISTINCT employer_name FROM staging.stg_vacancies WHERE employer_name IS NOT NULL ON CONFLICT (name) DO NOTHING;
INSERT INTO core.skills(name)    SELECT DISTINCT skill         FROM staging.stg_skills                                    ON CONFLICT (name) DO NOTHING;
TRUNCATE core.vacancy_skills, core.vacancies;
INSERT INTO core.vacancies(id,source,name,city_id,employer_id,salary_min,salary_max,salary_currency,
                           salary_gross,experience,schedule,is_remote,url,query)
SELECT s.id, s.source, s.name, c.id, e.id, s.salary_min, s.salary_max, s.salary_currency, s.salary_gross,
       s.experience, s.schedule, s.is_remote, s.url, s.query
FROM staging.stg_vacancies s
LEFT JOIN core.cities c    ON c.name = s.city_name
LEFT JOIN core.employers e ON e.name = s.employer_name;
INSERT INTO core.vacancy_skills(vacancy_id, skill_id)
SELECT k.vacancy_id, s.id FROM staging.stg_skills k JOIN core.skills s ON s.name = k.skill
ON CONFLICT DO NOTHING;
REFRESH MATERIALIZED VIEW mart.city_stats;
REFRESH MATERIALIZED VIEW mart.skill_demand;
REFRESH MATERIALIZED VIEW mart.salary_by_experience;
REFRESH MATERIALIZED VIEW mart.top_employers;
REFRESH MATERIALIZED VIEW mart.source_stats;
"""


class PostgresWarehouse:
    name = "postgres"

    def __init__(self, dsn: dict, schema_sql: Path):
        self.dsn = dsn
        self.schema_sql = Path(schema_sql)

    def _conn(self):
        return psycopg2.connect(**self.dsn)

    def init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(self.schema_sql.read_text(encoding="utf-8"))

    def load(self, vacancies: list[Vacancy]) -> int:
        vac_rows = [
            (v.id, v.source, v.name, v.city, v.employer, v.salary_min, v.salary_max,
             v.salary_currency, v.salary_gross, v.experience, v.schedule, v.is_remote,
             v.url, v.query)
            for v in vacancies
        ]
        skill_rows = [(v.id, s) for v in vacancies for s in v.skills]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE staging.stg_vacancies, staging.stg_skills")
            execute_values(
                cur, f"INSERT INTO staging.stg_vacancies ({', '.join(STG_COLUMNS)}) VALUES %s",
                vac_rows, page_size=BATCH_SIZE)
            execute_values(
                cur, "INSERT INTO staging.stg_skills (vacancy_id, skill) VALUES %s",
                skill_rows, page_size=BATCH_SIZE)
            cur.execute(LOAD_SQL)
            cur.execute("SELECT count(*) FROM core.vacancies")
            return cur.fetchone()[0]

    def count(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM core.vacancies")
            return cur.fetchone()[0]
