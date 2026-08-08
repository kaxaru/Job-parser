"""Адаптер MS SQL Server: типизированные записи -> staging -> core (звезда T-SQL) -> mart.

T-SQL-специфика: создание БД в master, батчи через GO, MERGE для upsert измерений,
NVARCHAR для кириллицы. Драйвер pymssql (без системного ODBC).
"""
from __future__ import annotations

import re
from pathlib import Path

from ..domain import Vacancy

# pymssql импортируется ЛЕНИВО (в _conn): драйвер опционален, его отсутствие
# не должно ломать импорт всего пакета (напр. в Airflow без mssql-таргета).

BATCH_SIZE = 1000
_GO = re.compile(r"(?im)^\s*GO\s*$")   # разделитель батчей T-SQL

STG_COLUMNS = ["id", "source", "name", "city_name", "employer_name", "salary_min", "salary_max",
               "salary_min_rub", "salary_max_rub",
               "salary_currency", "salary_gross", "experience", "schedule", "is_remote",
               "remote_mentioned", "url"]

# справочники (MERGE = upsert) -> факт -> мост. Витрины — views, REFRESH не нужен.
LOAD_SQL = """
MERGE core.cities AS t
USING (SELECT DISTINCT city_name FROM staging.stg_vacancies WHERE city_name IS NOT NULL) AS s
ON t.name = s.city_name
WHEN NOT MATCHED THEN INSERT(name) VALUES(s.city_name);

MERGE core.employers AS t
USING (SELECT DISTINCT employer_name FROM staging.stg_vacancies WHERE employer_name IS NOT NULL) AS s
ON t.name = s.employer_name
WHEN NOT MATCHED THEN INSERT(name) VALUES(s.employer_name);

MERGE core.skills AS t
USING (SELECT DISTINCT skill FROM staging.stg_skills) AS s
ON t.name = s.skill
WHEN NOT MATCHED THEN INSERT(name) VALUES(s.skill);

DELETE FROM core.vacancy_skills;
DELETE FROM core.vacancies;

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
SELECT k.vacancy_id, sk.id
FROM staging.stg_skills k JOIN core.skills sk ON sk.name = k.skill;
"""


class MSSQLWarehouse:
    name = "mssql"

    def __init__(self, dsn: dict, schema_sql: Path):
        self.dsn = dsn                       # server, port, user, password, database
        self.schema_sql = Path(schema_sql)

    def _conn(self, database: str | None = None, autocommit: bool = True):
        """autocommit=True — режим DDL: `CREATE DATABASE` в T-SQL внутри транзакции нельзя.
        Загрузка открывает соединение с autocommit=False, см. политику в `load`."""
        import pymssql  # ленивый импорт: нужен только при реальном использовании mssql
        d = dict(self.dsn)
        if database:
            d["database"] = database
        return pymssql.connect(charset="UTF-8", autocommit=autocommit, **d)

    def init_schema(self) -> None:
        db = self.dsn["database"]
        # Идентификатор в T-SQL параметром не передать, имя БД идёт в текст DDL. Значит,
        # проверяем сами: ']' закрывает скобочное квотирование `[db]`, кавычка рвёт литерал
        # внутри DB_ID('db'). Имя приходит из НАШЕГО конфига (MSSQL_DB), поэтому это fail
        # fast на дрейфе конфигурации, а не защита от пользовательского ввода; остальные
        # символы (дефис, пробел) законны и квотированием закрыты.
        if "]" in db or "'" in db:
            raise ValueError(f"недопустимое имя базы MSSQL_DB={db!r}: ']' и апостроф "
                             f"ломают квотирование идентификатора в T-SQL")
        # БД создаётся в контексте master (T-SQL: CREATE DATABASE нельзя в той же БД)
        with self._conn(database="master") as conn:
            conn.cursor().execute(f"IF DB_ID('{db}') IS NULL CREATE DATABASE [{db}]")
        # схема — побатчево (CREATE SCHEMA / VIEW обязаны быть первыми в батче)
        sql = self.schema_sql.read_text(encoding="utf-8")
        with self._conn() as conn, conn.cursor() as cur:
            for batch in (b.strip() for b in _GO.split(sql)):
                if batch:
                    cur.execute(batch)

    def load(self, vacancies: list[Vacancy]) -> int:
        """Полный перезалив факта ОДНОЙ транзакцией; возвращает число строк в факте.

        Политика восстановления: выполнено целиком или не выполнено вовсе — как у Postgres.
        Очистка staging, вставка батчей и `LOAD_SQL` (внутри которого
        `DELETE FROM core.vacancies`) идут в одной транзакции: соединение открыто с
        autocommit=False, `commit()` — только после успешного чтения итогового COUNT.
        Обрыв на любом шаге -> выход из `with` -> `Connection.close()`, который по
        документации pymssql делает неявный ROLLBACK, и в факте остаётся ПРЕДЫДУЩИЙ срез.
        Повтор безопасен: DELETE+INSERT идемпотентны.
        До 09.08.2026 соединение было в autocommit, и обрыв сразу после DELETE оставлял
        хранилище ПУСТЫМ — расхождение с PG было видно только человеку на дашборде.

        Осознанный компромисс: на время транзакции `core.*` заблокированы для читателей
        (Power BI и Metabase ждут либо видят прежний срез), тогда как в autocommit они
        видели бы пустую таблицу. Полный срез ~90 тыс. строк заливается минуты, не часы,
        и «подождать» лучше, чем «увидеть ноль».
        """
        # Порядок значений ОБЯЗАН совпадать с STG_COLUMNS — вставка позиционная.
        # BIT в MS SQL принимает 0/1, поэтому булевы приводятся к int, как и `is_remote`.
        vac_rows = [
            (v.id, v.source, v.name, v.city, v.employer, v.salary_min, v.salary_max,
             v.salary_min_rub, v.salary_max_rub,
             v.salary_currency, None if v.salary_gross is None else int(v.salary_gross),
             v.experience, v.schedule, int(v.is_remote), int(v.remote_mentioned),
             v.url)
            for v in vacancies
        ]
        skill_rows = [(v.id, s) for v in vacancies for s in v.skills]
        ph = ",".join(["%s"] * len(STG_COLUMNS))
        with self._conn(autocommit=False) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM staging.stg_skills; DELETE FROM staging.stg_vacancies;")
            for i in range(0, len(vac_rows), BATCH_SIZE):
                cur.executemany(
                    f"INSERT INTO staging.stg_vacancies ({','.join(STG_COLUMNS)}) VALUES ({ph})",
                    vac_rows[i:i + BATCH_SIZE])
            for i in range(0, len(skill_rows), BATCH_SIZE):
                cur.executemany(
                    "INSERT INTO staging.stg_skills (vacancy_id, skill) VALUES (%s, %s)",
                    skill_rows[i:i + BATCH_SIZE])
            cur.execute(LOAD_SQL)
            cur.execute("SELECT COUNT(*) FROM core.vacancies")
            in_fact = cur.fetchone()[0]     # читаем ДО commit: транзакция видит свою запись
            conn.commit()
            return in_fact

    def count(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM core.vacancies")
            return cur.fetchone()[0]
