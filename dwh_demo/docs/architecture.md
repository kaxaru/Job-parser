# Архитектура

## Что это за система

Демо-DWH уровня Data / Analytics Engineer на реальных данных: ~10k вакансий, собранных
парсером родительского проекта (`../data/vacancies_raw.json`).

Один и тот же extract + transform питает **три хранилища** — PostgreSQL, ClickHouse,
MS SQL. Все три показаны в одном BI (Metabase), оркестрация — Airflow, наблюдаемость —
Grafana + Loki.

Смысл проекта — не сами цифры по рынку труда, а демонстрация того, что смена движка
хранилища не требует переписывания конвейера.

## Ports & Adapters

Центральное решение. Порт — `warehouse/base.py::Warehouse`, `Protocol` из трёх методов:

```python
name: str
init_schema() -> None          # создать/обновить схему, идемпотентно
load(vacancies) -> int         # загрузить, вернуть число строк в факте
count() -> int                 # для верификации
```

`Pipeline` принимает список хранилищ как зависимость и не знает, куда грузит. Добавление
целого движка — это один адаптер плюс строка в реестре `pipeline.py::REGISTRY`:

```python
REGISTRY = {
    "postgres":   lambda cfg: PostgresWarehouse(cfg.pg_dsn, SQL_DIR / "postgres" / "schema.sql"),
    "clickhouse": lambda cfg: ClickHouseWarehouse(cfg.ch_url, SQL_DIR / "clickhouse" / "schema.sql"),
    "mssql":      lambda cfg: MSSQLWarehouse(cfg.mssql_dsn, SQL_DIR / "mssql" / "schema.sql"),
}
TARGETS = tuple(REGISTRY)
```

Проверяется тем, что MS SQL добавлялся последним и потребовал ровно этого. Airflow-DAG
тоже строит таски из `TARGETS` — новый бэкенд появляется в графе сам.

## Поток данных

```
../data/vacancies_raw.json          (парсер HH, родительский проект)
        │
        ▼
   JsonSource.read()                 extract
        │
        ▼
   Vacancy.from_raw()                transform — ОДИН раз, в Python
   разбор зарплат, опыт/график,      дедуп по id
   37 regex-навыков, is_remote
        │
        ├──────────────┬──────────────────┬─────────────────┐
        ▼              ▼                  ▼                 │
   PostgreSQL      ClickHouse         MS SQL                │  load (fan-out)
   staging         широкий факт       staging               │
     → core          + Aggregating      → core (T-SQL)      │
       (звезда)        MergeTree MV       (звезда)          │
     → mart                             → mart (views)      │
       (matview,     витрины на           MERGE/IDENTITY    │
        REFRESH)      вставке                               │
        │              │                  │                 │
        └──────────────┴──────────────────┴─────────────────┘
                       ▼
                   Metabase  (дашборды /2 /3 /4 /5)
                       +
              Power BI (опционально, поверх MS SQL, вне Docker)
```

**Transform делается один раз** и кэшируется в `Pipeline.prepare()`. Это принципиально:
если бы каждый адаптер парсил сам, три хранилища могли бы разойтись в данных при одинаковом
входе, а сравнение движков потеряло бы смысл.

## Слои хранилища

Классическая трёхслойка, одинаковая по смыслу во всех движках:

**staging** — типизированный приём. Сырого JSONB-слоя нет намеренно: transform уже сделан
в Python, в staging прилетают готовые колонки.

**core** — звезда: факт `vacancies` плюс измерения `cities`, `employers`, `skills` и мост
`vacancy_skills` (связь многие-ко-многим по навыкам).

**mart** — витрины: `city_stats`, `skill_demand`, `salary_by_experience`, `top_employers`.

Исключение — ClickHouse: там вместо звезды **широкий факт** с массивом `skills`. Это не
непоследовательность, а демонстрация разницы парадигм; подробности в
[`warehouses.md`](warehouses.md).

## Чем витрины отличаются между движками

Один и тот же результат достигается тремя способами — в этом половина ценности проекта:

- **PostgreSQL** — материализованные представления, обновляются явным `REFRESH MATERIALIZED
  VIEW` на шаге load
- **ClickHouse** — `AggregatingMergeTree` плюс инкрементальные MV, витрины наполняются
  **на каждой вставке**, REFRESH не нужен вовсе
- **MS SQL** — обычные views (всегда live) плюс `MERGE` для upsert измерений

## Слои кода

```
etl/
  domain.py       Vacancy + from_raw, справочники, regex навыков — БЕЗ БД и I/O
  source.py       JsonSource — extract
  pipeline.py     Pipeline + REGISTRY + build_pipeline — оркестрация
  config.py       Settings.from_env — DSN всех бэкендов
  cli.py          python -m etl
  warehouse/      base.py (порт) + postgres/clickhouse/mssql (адаптеры)
  sql/            schema.sql на каждый движок
bi/               провижининг Metabase из кода
dags/             Airflow DAG
observability/    Grafana + Loki как код
search_demo/      отдельный PoC полнотекстового поиска
```

`domain.py` — чистые функции без БД: переиспользуется всеми адаптерами и тестируется
без контейнеров. Это то же разделение, что и в родительском проекте.

## Идемпотентность

Все операции рассчитаны на повторный запуск:

- `init_schema` — `CREATE ... IF NOT EXISTS` во всех движках
- `load` — полный перезалив: staging чистится, `core.vacancies` пересоздаётся,
  измерения добираются через `ON CONFLICT DO NOTHING` (PG) или `MERGE` (MS SQL)
- провижининг BI — повторный запуск архивирует старые карточки и пересобирает, без дублей

**Дедуп на входе** (`Pipeline.prepare`) обязателен: одна вакансия приходит из нескольких
поисковых запросов (`_query`), и дубль по id уронил бы PRIMARY KEY в PG и MS SQL, а в
ClickHouse молча задвоил бы счётчики.

## Границы

Входит: extract из JSON, transform, загрузка в три движка, схемы, витрины, провижининг
дашбордов, DAG, observability-стек.

Не входит: сбор вакансий (это родительский проект), аналитика поверх витрин (это Metabase),
Power BI Desktop (Windows-приложение, контейнеризовать нельзя).

## Карта документации

- [`etl.md`](etl.md) — конвейер, домен, CLI
- [`warehouses.md`](warehouses.md) — три адаптера и схемы данных
- [`orchestration.md`](orchestration.md) — Airflow
- [`bi.md`](bi.md) — Metabase и Power BI
- [`observability.md`](observability.md) — Grafana, Loki, Promtail, MinIO
- [`search.md`](search.md) — PoC полнотекстового поиска и его измерения
- [`deployment.md`](deployment.md) — docker-compose, порты, сброс
- [`config.md`](config.md) — переменные окружения
- [`testing.md`](testing.md) — тесты и критерии приёмки
