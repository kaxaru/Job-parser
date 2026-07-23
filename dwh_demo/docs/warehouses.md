# Хранилища — три адаптера

Заменяет привычный `database.md`: здесь три движка, и половина ценности проекта в том,
чем именно они отличаются.

## Порт

`warehouse/base.py::Warehouse` — `Protocol`, помеченный `@runtime_checkable`:

```python
name: str
init_schema() -> None
load(vacancies: list[Vacancy]) -> int
count() -> int
```

`Pipeline` работает только с этим контрактом. Адаптеры не наследуются от базового класса —
структурная типизация, соответствие проверяется тестом.

## Общая модель данных

Одинакова для PostgreSQL и MS SQL, различается у ClickHouse.

**staging** — типизированный приём, 14 колонок:

```
staging.stg_vacancies (id, source, name, city_name, employer_name, salary_min, salary_max,
                       salary_currency, salary_gross, experience, schedule,
                       is_remote, url, query)
staging.stg_skills    (vacancy_id, skill)
```

`id` — TEXT/String/NVARCHAR (не BIGINT/UInt64): основной проект неймспейсит id по
источникам (`hirify_733072`, `talanto_<uuid>`). `source` — портал (hh/hirify/talanto).

**core** — звезда:

```
core.vacancies       факт: id, source, name, city_id, employer_id, зарплата, опыт,
                     schedule, is_remote, url, query, loaded_at
core.cities          измерение (id, name UNIQUE)
core.employers       измерение (id, name UNIQUE)
core.skills          измерение (id, name UNIQUE)
core.vacancy_skills  мост (vacancy_id, skill_id) — многие-ко-многим
```

**mart** — пять витрин: `city_stats`, `skill_demand`, `salary_by_experience`,
`top_employers`, `source_stats` (объём/зарплата/remote в разрезе портала).

Пример определения витрины (`sql/postgres/schema.sql`):

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.city_stats AS
SELECT c.name AS city, count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary,
       round(avg(v.salary_min)) AS avg_salary_min,
       round(avg(v.salary_max)) AS avg_salary_max,
       round(100.0 * count(*) FILTER (WHERE v.is_remote) / count(*), 1) AS remote_share_pct
FROM core.vacancies v JOIN core.cities c ON c.id = v.city_id
GROUP BY c.name;
```

---

## PostgreSQL

**Модуль.** `warehouse/postgres.py::PostgresWarehouse`
**Драйвер.** `psycopg2` + `execute_values`
**Схема.** `sql/postgres/schema.sql`

**Как грузит.** `execute_values` многострочными INSERT по `BATCH_SIZE = 1000` в staging,
затем один пакет SQL (`postgres.py::LOAD_SQL`): справочники -> факт -> мост -> REFRESH витрин.

```sql
INSERT INTO core.cities(name) SELECT DISTINCT city_name FROM staging.stg_vacancies
  WHERE city_name IS NOT NULL ON CONFLICT (name) DO NOTHING;
TRUNCATE core.vacancy_skills, core.vacancies;
INSERT INTO core.vacancies(...) SELECT ... FROM staging.stg_vacancies s
  LEFT JOIN core.cities c ON c.name = s.city_name ...;
REFRESH MATERIALIZED VIEW mart.city_stats;
```

**Витрины.** Материализованные, обновляются явным `REFRESH` в конце load.

**Идемпотентность.** `TRUNCATE` факта и моста перед вставкой; измерения добираются
через `ON CONFLICT DO NOTHING` — накопительно, id сохраняются между прогонами.

**Edge cases**

- `LEFT JOIN` на измерения: вакансия без города или работодателя не теряется,
  получает `NULL` в `city_id`
- `loaded_at` со `DEFAULT now()` — отметка времени загрузки, единственное поле,
  добавляемое хранилищем
- Индексы по `city_id`, `employer_id`, `is_remote` создаются в схеме

**Почему так.** `ON CONFLICT DO NOTHING` вместо пересоздания измерений: их id ссылаются
из моста, и пересборка ломала бы связи. Факт при этом перезаливается целиком — так проще
и данные малы.

---

## ClickHouse

**Модуль.** `warehouse/clickhouse.py::ClickHouseWarehouse`
**Драйвер.** нет — HTTP через `urllib` из stdlib
**Схема.** `sql/clickhouse/schema.sql`

**Модель отличается: широкий факт вместо звезды.**

```
hh.vacancies      ENGINE = MergeTree
                  id, source, name, city, employer, зарплата, experience, schedule,
                  is_remote, url, query, skills Array(String)
hh.skill_demand   ENGINE = AggregatingMergeTree  <- hh.skill_demand_mv
hh.salary_by_exp  ENGINE = AggregatingMergeTree  <- hh.salary_by_exp_mv
hh.source_stats   ENGINE = AggregatingMergeTree  <- hh.source_stats_mv (объём/зарплата/remote × источник)
```

Навыки лежат массивом в самой строке, отдельных таблиц-измерений и моста нет. Для
колоночной СУБД джойны дороже, а массив разворачивается через `arrayJoin` — это идиома
движка, а не упрощение.

**Как грузит.** `TRUNCATE` факта и витрин, затем батчи по `BATCH = 2000` строк в формате
`JSONEachRow`:

```python
self._post("INSERT INTO hh.vacancies FORMAT JSONEachRow", "\n".join(batch))
```

**Витрины.** Инкрементальные MV поверх `AggregatingMergeTree` — наполняются **на каждой
вставке**, `REFRESH` не существует как понятие.

**Edge cases**

- `init_schema` бьёт файл по `;` вручную: ClickHouse по HTTP исполняет **по одному**
  стейтменту. Перед разбиением срезаются `--`-комментарии, иначе `;` внутри комментария
  ломает разбиение
- Булевы приводятся к `int`: `int(v.is_remote)`, `None if v.salary_gross is None else
  int(...)` — сохраняется различие «нет данных» и «False»
- Таймаут HTTP-запроса 180 с — вставка батча на 2000 строк может быть долгой

**Почему так.** Отказ от клиентской библиотеки — сознательный: HTTP-интерфейс ClickHouse
самодостаточен, а `requirements.txt` остаётся из двух строк.

---

## MS SQL Server

**Модуль.** `warehouse/mssql.py::MSSQLWarehouse`
**Драйвер.** `pymssql`, импортируется **лениво** внутри `_conn`
**Схема.** `sql/mssql/schema.sql`

**Модель.** Такая же звезда, как в PostgreSQL, но на T-SQL: `IDENTITY(1,1)` вместо
`SERIAL`, `NVARCHAR` вместо `TEXT` (обязательно — иначе кириллица превращается в `?`).

**Как грузит.** `DELETE` staging, затем `executemany` батчами по 1000, затем
`mssql.py::LOAD_SQL` с `MERGE` для каждого измерения:

```sql
MERGE core.cities AS t
USING (SELECT DISTINCT city_name FROM staging.stg_vacancies WHERE city_name IS NOT NULL) AS s
ON t.name = s.city_name
WHEN NOT MATCHED THEN INSERT(name) VALUES(s.city_name);
```

**Витрины.** Обычные views — всегда актуальны, обновлять нечего.

**Edge cases**

- **`CREATE DATABASE` только из `master`** — T-SQL не позволяет создать БД, находясь
  в ней же. Отсюда отдельное подключение: `_conn(database="master")`
- **Батчи через `GO`** — `CREATE SCHEMA` и `CREATE VIEW` обязаны быть первыми
  в батче, поэтому файл схемы режется регуляркой `_GO` и исполняется по частям
- **`DELETE` вместо `TRUNCATE`** — `TRUNCATE` не работает при внешних ключах
- **Ленивый импорт `pymssql`** — драйвер опционален; его отсутствие не должно ломать
  импорт всего пакета, например в Airflow без mssql-таргета
- **Города-агрегаторы режутся в домене** — `domain.py::_cap` обрезает city до
  `CITY_MAX = 200` code points (talanto шлёт списки стран до ~2400 симв.); одинаково для всех
  движков, поэтому числа сходятся. City-колонки MSSQL при этом `NVARCHAR(450)`, а не (200):
  эмодзи-флаги (🇦🇩 = суррогатные пары) раздувают 200 code points до ~400 UTF-16 юнитов
- **Медленнее остальных** — `executemany` вместо bulk-загрузки

**Почему так.** MS SQL добавлен, чтобы показать работу с Microsoft-стеком и T-SQL-специфику
(`MERGE`, `IDENTITY`, `NVARCHAR`), а также дать источник для Power BI.

---

## Сравнение

Одно и то же, тремя способами:

**Модель.** PG и MS SQL — звезда с мостом; ClickHouse — широкий факт с массивом.

**Обновление витрин.** PG — явный `REFRESH` на шаге load; ClickHouse — автоматически на
вставке; MS SQL — не требуется, views живые.

**Upsert измерений.** PG — `ON CONFLICT DO NOTHING`; MS SQL — `MERGE`; ClickHouse —
не применимо, измерений нет.

**Транспорт.** PG — `psycopg2`; MS SQL — `pymssql` (ленивый); ClickHouse — stdlib `urllib`.

**Очистка перед заливкой.** PG — `TRUNCATE`; MS SQL — `DELETE` (мешают FK);
ClickHouse — `TRUNCATE TABLE IF EXISTS`.

## Проверка

```bash
python -m etl -t postgres all
python -m etl -t clickhouse all
python -m etl -t mssql all
python -m pytest -m integration     # ВНИМАНИЕ: TRUNCATE'ит БД
```

Ожидаемо: `[etl] [<backend>] schema ready`, затем `[etl] [<backend>] loaded: N`,
где N одинаково для всех трёх движков — это и есть проверка, что адаптеры согласованы.
