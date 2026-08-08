# HH DWH — ETL → PostgreSQL / ClickHouse / MS SQL → Metabase + Airflow + Grafana/Loki

Портфолио-проект уровня Data / Analytics Engineer на реальных данных вакансий
из кеша родительского проекта (`../data/vacancies_raw.json`, ~460 МБ). Порталы задаёт
родитель — `hrwork/config.py::SOURCES`, по умолчанию их девять (hh, hirify, talanto,
getmatch, arbeitnow, himalayas, web3, themuse, jobicy); стенд ничего не собирает сам
и берёт срез как есть.

Один и тот же extract + transform питает **три хранилища**, все показаны в одном BI,
оркестрация — Airflow, наблюдаемость — Grafana + Loki.

## Что демонстрирует

- **ETL по слоям** `staging → core → mart` (звезда + витрины) на **трёх движках**
- **Ports & Adapters** — бэкенд хранилища инжектируется; добавление движка = один адаптер
  плюс строка в реестре
- **Microsoft-стек** — MS SQL с T-SQL (`IDENTITY`, `MERGE`, `NVARCHAR`, views) в Docker;
  Power BI / DAX — отдельная инструкция, Desktop ставится вручную
- **Автоматизация** — Airflow DAG `hh_etl`: три независимые цепочки `init_<t> -> load_<t>`
  по одной на бэкенд (6 тасок), выполняются по очереди
- **BI как код** — дашборды Metabase провижатся скриптом, идемпотентно
- **Observability как код** — Grafana + Loki + Promtail, Loki на S3 (MinIO) с retention;
  дашборд «здоровье пайплайна»
- **Текст-майнинг** — стек берётся из кеша родителя, когда сигнатура его словаря совпала
  с пином, иначе размечается своими регэкспами (37 = 19 копий словаря родителя +
  18 аналитических тегов стенда)
- **Тесты** — unit без БД плюс integration на живых Postgres, ClickHouse и MS SQL (+ CI)

> **Две ниши BI на одном проекте.** Metabase — аналитика данных (категориальные срезы),
> Grafana — операционный взгляд (метрики Airflow и логи из Loki).

## Архитектура

```
                       ┌─ extract + transform (Python, ОДИН раз) ──┐
 vacancies_raw.json ──►│  Vacancy.from_raw: разбор зарплат и их    │
   (сбор родителя)     │  перевод в рубли, опыт/график, стек,      │
 fx_rates.json ───────►│  is_remote (remote + гибрид)              │
   (суточный кеш)      └──────┬──────────────┬──────────────┬──────┘
                          load (fan-out, один вход — три бэкенда)
              ┌───────────────▼───┐ ┌────────▼────────┐ ┌───────▼──────────┐
              │ PostgreSQL        │ │ ClickHouse      │ │ MS SQL (T-SQL)   │
              │ core(звезда)→mart │ │ широкий факт →  │ │ core(звезда)→    │
              │ matview, REFRESH  │ │ Aggregating-MT  │ │ mart (views),    │
              │                   │ │ (on-insert)     │ │ MERGE/IDENTITY   │
              └─────────┬─────────┘ └────────┬────────┘ └───────┬──────────┘
                        └──────────► Metabase ◄──────────────────┘
                             (дашборды /2, /3, /4, /5, /6-sources)
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
python -m pytest                   # 220 unit; integration отфильтрованы
docker compose down -v             # полный сброс, включая тома
```

Ожидаемый вывод `python -m etl all` (N — сколько уникальных вакансий дал кеш родителя
на этом прогоне, D — сколько повторов id отсеял дедуп, K — курсов в кеше, M — вакансий
с рублёвой вилкой):

```
[etl] [postgres] schema ready
[etl] [clickhouse] schema ready
[etl] [mssql] schema ready
[etl] prepare: N вакансий (extract+transform); пропущено: no_id=0 broken=0 dup=D; FX: K курсов, вилка в рублях у M
[etl] [postgres] loaded: N
[etl] [clickhouse] loaded: N
[etl] [mssql] loaded: N
[etl] verify: prepare=N, в факте {'postgres': N, 'clickhouse': N, 'mssql': N}
```

Одинаковое N во всех строках — главная проверка согласованности адаптеров, и с 09.08.2026
её делает не глаз, а `Pipeline._verify`: расхождение роняет прогон и называет отставший
движок. Счётчик `broken` в норме близок к нулю: доля неразобранных выше 1 % означает смену
формата данных у родителя, и прогон падает, не перезаливая факт.

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

Шаблоны спеки и RFC — общие с родительским проектом: `../docs/template/spec-template.md`
и `../docs/template/rfc-template.md`. Каталог локальный, в `.gitignore` — в клоне его нет.

## Структура

```
dwh_demo/
├─ docker-compose.yml   12 сервисов: 3×DWH + BI + Airflow + Grafana/Loki/Promtail/MinIO
├─ etl/                 ETL (Ports & Adapters)
│  ├─ domain.py         Vacancy + from_raw, навыки — БЕЗ БД
│  ├─ rates.py          курсы валют из суточного кеша родителя, БЕЗ СЕТИ
│  ├─ source.py         JsonSource
│  ├─ pipeline.py       Pipeline + REGISTRY + build_pipeline + санити-гейты и verify
│  ├─ config.py         Settings.from_env
│  ├─ cli.py            python -m etl
│  ├─ warehouse/        base (порт) + postgres · clickhouse · mssql
│  └─ sql/              schema.sql на движок
├─ bi/                  провижининг Metabase (python -m bi)
│  ├─ client.py         фасад REST
│  ├─ registry.py       реестр дашбордов
│  └─ dashboards/       base · overview · comparison · cooccurrence · mssql_overview · source_comparison
├─ dags/hh_etl_dag.py   Airflow DAG (fan-out)
├─ observability/       Grafana + Loki как код
├─ search_demo/         PoC поиска: load.py, bench.py, REPORT.md
├─ powerbi/README.md    Power BI Desktop → MS SQL + DAX-меры
├─ postgres/init/       авто-создание БД airflow
├─ tests/               unit + integration + fixtures
├─ docs/                документация
├─ conftest.py          sys.path + синтетические курсы в юнит-прогоне + авто-маркер unit
└─ ruff.toml · pytest.ini · requirements*.txt
```
