"""Адаптер Postgres: типизированные записи -> staging.stg_* -> core (звезда) ->
REFRESH mart.* (материализованные витрины)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

from ..domain import Vacancy

BATCH_SIZE = 1000   # строк в одном многострочном INSERT (баланс round-trip / память)

STG_COLUMNS = ["id", "source", "name", "city_name", "employer_name", "salary_min", "salary_max",
               "salary_min_rub", "salary_max_rub",
               "salary_currency", "salary_gross", "experience", "schedule", "is_remote",
               "remote_mentioned", "url"]

# справочники -> факт -> мост -> обновление витрин
LOAD_SQL = """
INSERT INTO core.cities(name)    SELECT DISTINCT city_name     FROM staging.stg_vacancies WHERE city_name     IS NOT NULL ON CONFLICT (name) DO NOTHING;
INSERT INTO core.employers(name) SELECT DISTINCT employer_name FROM staging.stg_vacancies WHERE employer_name IS NOT NULL ON CONFLICT (name) DO NOTHING;
INSERT INTO core.skills(name)    SELECT DISTINCT skill         FROM staging.stg_skills                                    ON CONFLICT (name) DO NOTHING;
TRUNCATE core.vacancy_skills, core.vacancies;
INSERT INTO core.vacancies(id,source,name,city_id,employer_id,salary_min,salary_max,
                           salary_min_rub,salary_max_rub,salary_currency,
                           salary_gross,experience,schedule,is_remote,remote_mentioned,url)
SELECT s.id, s.source, s.name, c.id, e.id, s.salary_min, s.salary_max,
       s.salary_min_rub, s.salary_max_rub, s.salary_currency, s.salary_gross,
       s.experience, s.schedule, s.is_remote, s.remote_mentioned, s.url
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

    def __init__(self, dsn: dict[str, str | int], schema_sql: Path):
        self.dsn = dsn
        self.schema_sql = Path(schema_sql)

    def _conn(self) -> Any:
        # Any — внешняя граница: у psycopg2 нет стабов, соединение и курсор приходят Any.
        return psycopg2.connect(**self.dsn)

    @contextmanager
    def _session(self) -> Iterator[Any]:
        """Курсор в транзакции + ГАРАНТИРОВАННОЕ закрытие соединения.

        У psycopg2 `with conn` управляет ТРАНЗАКЦИЕЙ (commit на выходе, rollback на
        исключении), а соединение НЕ закрывает. Без явного `close()` соединения копятся
        в долгоживущем воркере Airflow до сборки мусора; с 09.08.2026 их стало больше —
        санити-гейт конвейера зовёт `count()` перед каждой загрузкой."""
        conn = self._conn()
        try:
            with conn, conn.cursor() as cur:
                yield cur
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._session() as cur:
            cur.execute(self.schema_sql.read_text(encoding="utf-8"))

    def load(self, vacancies: list[Vacancy]) -> int:
        """Полный перезалив факта одной транзакцией; возвращает число строк в факте.

        Политика восстановления: выполнено целиком или не выполнено вовсе. TRUNCATE
        staging, вставка и `LOAD_SQL` (внутри — `TRUNCATE core.*` и REFRESH витрин) идут
        в одной транзакции psycopg2: исключение -> выход из `with conn` -> ROLLBACK,
        в факте остаётся предыдущий срез. Повтор безопасен (полная замена факта).
        """
        # Порядок значений ОБЯЗАН совпадать с STG_COLUMNS — позиционная вставка.
        vac_rows = [
            (v.id, v.source, v.name, v.city, v.employer, v.salary_min, v.salary_max,
             v.salary_min_rub, v.salary_max_rub,
             v.salary_currency, v.salary_gross, v.experience, v.schedule, v.is_remote,
             v.remote_mentioned, v.url)
            for v in vacancies
        ]
        skill_rows = [(v.id, s) for v in vacancies for s in v.skills]
        with self._session() as cur:
            cur.execute("TRUNCATE staging.stg_vacancies, staging.stg_skills")
            execute_values(
                cur, f"INSERT INTO staging.stg_vacancies ({', '.join(STG_COLUMNS)}) VALUES %s",
                vac_rows, page_size=BATCH_SIZE)
            execute_values(
                cur, "INSERT INTO staging.stg_skills (vacancy_id, skill) VALUES %s",
                skill_rows, page_size=BATCH_SIZE)
            cur.execute(LOAD_SQL)
            cur.execute("SELECT count(*) FROM core.vacancies")
            # Курсор без стабов отдаёт Any; приводим здесь, чтобы драйвер, вернувший
            # не число, падал на границе адаптера, а не в сверке `Pipeline._verify`.
            return int(cur.fetchone()[0])

    def count(self) -> int:
        with self._session() as cur:
            cur.execute("SELECT count(*) FROM core.vacancies")
            return int(cur.fetchone()[0])
