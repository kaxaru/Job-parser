# HH DWH — ETL → PostgreSQL / ClickHouse / MS SQL → Metabase + Airflow + Grafana/Loki

Портфолио-проект уровня Data / Analytics Engineer на реальных данных вакансий HH
(`../data/vacancies_raw.json`, ~10k записей от парсера родительского проекта).

Один и тот же extract + transform питает **три хранилища**, все показаны в одном BI,
оркестрация — Airflow, наблюдаемость — Grafana + Loki.

## Что демонстрирует

- **ETL по слоям** `staging → core → mart` (звезда + витрины) на **трёх движках**
- **Ports & Adapters** — бэкенд хранилища инжектируется; добавление движка = один адаптер
  плюс строка в реестре
- **Microsoft-стек** — MS SQL с T-SQL (`IDENTITY`, `MERGE`, `NVARCHAR`, views) в Docker;
  Power BI / DAX — отдельная инструкция, Desktop ставится вручную
- **Автоматизация** — Airflow DAG `hh_etl` с fan-out `init → [load_postgres,
  load_clickhouse, load_mssql]`
- **BI как код** — дашборды Metabase провижатся скриптом, идемпотентно
- **Observability как код** — Grafana + Loki + Promtail, Loki на S3 (MinIO) с retention;
  дашборд «здоровье пайплайна»
- **Текст-майнинг** — разметка 37 навыков регэкспами и флаг удалёнки по тексту
- **Тесты** — unit без БД плюс integration на живых Postgres и ClickHouse

> **Две ниши BI на одном проекте.** Metabase — аналитика данных (категориальные срезы),
> Grafana — операционный взгляд (метрики Airflow и логи из Loki).

## Архитектура

```
                       ┌─ extract + transform (Python, ОДИН раз) ──┐
 vacancies_raw.json ──►│  Vacancy.from_raw: разбор зарплат,        │
   (парсер HH)         │  опыт/график, regex-навыки, is_remote     │
                       └──────┬──────────────┬──────────────┬──────┘
                          load (fan-out, один вход — три бэкенда)
              ┌───────────────▼───┐ ┌────────▼────────┐ ┌───────▼──────────┐
              │ PostgreSQL        │ │ ClickHouse      │ │ MS SQL (T-SQL)   │
              │ core(звезда)→mart │ │ широкий факт →  │ │ core(звезда)→    │
              │ matview, REFRESH  │ │ Aggregating-MT  │ │ mart (views),    │
              │                   │ │ (on-insert)     │ │ MERGE/IDENTITY   │
              └─────────┬─────────┘ └────────┬────────┘ └───────┬──────────┘
                        └──────────► Metabase ◄──────────────────┘
                             (дашборды /2, /3, /4, /5)
            (Power BI/DAX — опционально, поверх MS SQL, вне Docker)
```

Витрины обновляются по-разному: Postgres — `REFRESH MATERIALIZED VIEW` на шаге load,
ClickHouse — инкрементальные MV на каждой вставке, MS SQL — обычные views плюс `MERGE`
для upsert измерений. Почему так — [`docs/warehouses.md`](docs/warehouses.md).

## Быстрый старт

venv активирован (см. корневой [`README`](../README.md)) — поэтому ниже просто `python`:

```bash
cd dwh_demo
docker compose up -d                          # 1. поднять стек (12 сервисов)
python -m pip install -r requirements-dev.txt # 2. зависимости (один раз)
python -m etl all                             # 3. данные во все хранилища
python -m bi all                              # 4. дашборды Metabase
```

Открыть: `http://localhost:3000/dashboard/2` (`demo@hh.local` / `DwhDemo2026!`).

Порты, логины, минимальные наборы сервисов и разбор проблем —
[`docs/deployment.md`](docs/deployment.md).

## Основные команды

```bash
python -m etl all                  # init схем + load во все бэкенды
python -m etl -t postgres load     # только Postgres, только загрузка
python -m bi overview              # пересобрать один дашборд
python -m pytest                   # 36 unit; integration отфильтрованы
docker compose down -v             # полный сброс, включая тома
```

Ожидаемый вывод `python -m etl all`:

```
[etl] [postgres] schema ready
[etl] [clickhouse] schema ready
[etl] [mssql] schema ready
[etl] prepare: 9831 вакансий (extract+transform)
[etl] [postgres] loaded: 9831
[etl] [clickhouse] loaded: 9831
[etl] [mssql] loaded: 9831
```

Совпадение чисел по всем бэкендам — главная проверка согласованности адаптеров.

## Документация

- [`architecture.md`](docs/architecture.md) — Ports & Adapters, слои, поток данных.
  **Начинать отсюда**
- [`etl.md`](docs/etl.md) — конвейер, доменная модель, CLI
- [`warehouses.md`](docs/warehouses.md) — три адаптера, схемы, чем движки отличаются
- [`orchestration.md`](docs/orchestration.md) — Airflow DAG
- [`bi.md`](docs/bi.md) — Metabase и Power BI
- [`observability.md`](docs/observability.md) — Grafana, Loki, Promtail, MinIO
- [`search.md`](docs/search.md) — PoC полнотекстового поиска и измерения
- [`deployment.md`](docs/deployment.md) — сервисы, порты, сброс, типичные проблемы
- [`config.md`](docs/config.md) — переменные окружения
- [`testing.md`](docs/testing.md) — тесты и критерии приёмки

Шаблоны спеки и RFC — общие с родительским проектом:
[`../docs/spec-template.md`](../docs/spec-template.md),
[`../docs/rfc-template.md`](../docs/rfc-template.md).

## Структура

```
dwh_demo/
├─ docker-compose.yml   12 сервисов: 3×DWH + BI + Airflow + Grafana/Loki/Promtail/MinIO
├─ etl/                 ETL (Ports & Adapters)
│  ├─ domain.py         Vacancy + from_raw, навыки — БЕЗ БД
│  ├─ source.py         JsonSource
│  ├─ pipeline.py       Pipeline + REGISTRY + build_pipeline
│  ├─ config.py         Settings.from_env
│  ├─ cli.py            python -m etl
│  ├─ warehouse/        base (порт) + postgres · clickhouse · mssql
│  └─ sql/              schema.sql на движок
├─ bi/                  провижининг Metabase (python -m bi)
│  ├─ client.py         фасад REST
│  ├─ registry.py       реестр дашбордов
│  └─ dashboards/       base · overview · comparison · cooccurrence · mssql_overview
├─ dags/hh_etl_dag.py   Airflow DAG (fan-out)
├─ observability/       Grafana + Loki как код
├─ search_demo/         PoC поиска: load.py, bench.py, REPORT.md
├─ powerbi/README.md    Power BI Desktop → MS SQL + DAX-меры
├─ postgres/init/       авто-создание БД airflow
├─ tests/               unit + integration + fixtures
├─ docs/                документация
└─ ruff.toml · pytest.ini · requirements*.txt
```
