# BI — Metabase и Power BI

## Metabase — дашборды как код

**Модули:** `bi/` — `client.py`, `config.py`, `cli.py`, `registry.py`, `dashboards/`
**UI:** `http://localhost:3000` (`demo@hh.local` / `DwhDemo2026!`)

Дашборды не собираются кликами: они описаны кодом и провижатся идемпотентно. Повторный
запуск архивирует старые карточки и пересобирает — без дублей.

### Команды

```bash
python -m bi                # = all: все дашборды
python -m bi all
python -m bi overview       # /dashboard/2
python -m bi comparison     # /dashboard/3
python -m bi cooccurrence   # /dashboard/4
python -m bi mssql          # /dashboard/5
python -m bi sources        # источники: hh vs hirify vs talanto
python -m bi --help
```

### Фасад Metabase

`bi/client.py::MetabaseClient` — обёртка над REST API:

```python
connect()                                        # логин, сессия
find_database(name, engine)                      # найти подключение
ensure_database(name, engine, details) -> int    # создать, если нет
run_sql(db_id, sql) -> list
create_card(name, db_id, sql, display, viz, tags) -> int
upsert_dashboard(title) -> int                   # идемпотентно
set_dashboard(d_id, dashcards, parameters)
```

`ensure_database` и `upsert_dashboard` — ключ к идемпотентности: подключения к трём
движкам и сами дашборды не дублируются между запусками.

### Реестр дашбордов

`bi/registry.py` — то же решение, что и в ETL:

```python
REGISTRY: dict[str, type[Dashboard]] = {
    cls.key: cls for cls in (
        OverviewDashboard, ComparisonDashboard, CooccurrenceDashboard, MssqlOverviewDashboard,
        SourceComparisonDashboard,
    )
}
```

Добавить дашборд = добавить класс в кортеж. Ключ CLI берётся из атрибута самого класса.

`dashboards/base.py::Dashboard` — `Protocol` с `key`, `title` и `build(client)`.
Тип реестра `dict[str, type[Dashboard]]` даёт статическую проверку соответствия,
рантайм-проверку — тест.

Хелперы разметки там же: `layout()` раскладывает карточки по сетке, `bar()` собирает
описание столбчатой визуализации, `text_tag()` — параметр-фильтр.

### Дашборды

**`/dashboard/2` — Обзор рынка.** 7 карточек, фильтры «Город» и «Опыт». Источник —
витрины PostgreSQL.

**`/dashboard/3` — Postgres vs ClickHouse.** 14 карточек: 7 метрик × 2 движка, попарно
рядом. Смысл — показать, что одни и те же цифры получаются из разных моделей данных
(звезда против широкого факта).

**`/dashboard/4` — Со-встречаемость навыков.** Дропдаун навыка -> что ищут вместе с ним.
Self-join по мосту `core.vacancy_skills`.

**`/dashboard/5` — MS SQL / T-SQL.** Обзор на Microsoft-стеке, 7 карточек.

**Источники — hh vs hirify vs talanto** (`python -m bi sources`). 5 карточек в разрезе
`source`: объём, медиана зарплатной вилки, доля remote, покрытие зарплатой, топ-навыки ×
источник. Читает витрину `mart.source_stats` (объём/зарплата/remote) и `core` (навыки).
Не путать с `/dashboard/3`: тот сравнивает ДВИЖКИ на одних данных, этот — ПОРТАЛЫ внутри
одного движка. Появился после подключения talanto.

### Крайние случаи

- **Metabase не поднялся** — `connect()` падает; проверять `curl localhost:3000/api/health`
- **Витрины пустые** — карточки создадутся, но покажут пустоту: сначала `python -m etl all`
- **«user already exists»** — том Metabase не пуст, см. [`deployment.md`](deployment.md)
- **ClickHouse-карточки пустые** при живом Postgres — не прогонялся
  `python -m etl -t clickhouse all`

---

## Power BI — вне Docker

**Инструкция:** `powerbi/README.md`

Power BI Desktop — Windows-приложение, контейнеризовать нельзя, и в этом стеке оно
**не установлено**. В репозитории лежит только подготовка: как поставить Desktop,
подключиться к `hh-mssql`, и готовые DAX-меры (`CALCULATE`, `DIVIDE`, `MEDIAN`).

Сам `.pbix` собирается в Desktop вручную. Источник — звезда `core` в MS SQL, та же,
что питает дашборд `/dashboard/5`.

## Две ниши BI на одном проекте

Разделение намеренное и стоит того, чтобы его понимать:

**Metabase** — аналитика данных: категориальные срезы, сравнение движков,
со-встречаемость навыков. Вопрос «что на рынке».

**Grafana** — операционный взгляд: time-series метрики прогонов Airflow и логи из Loki.
Вопрос «жив ли пайплайн». См. [`observability.md`](observability.md).

## Проверка

```bash
python -m bi all
python -m pytest tests/test_bi_client.py -q
```

Ожидаемо: дашборды доступны по `/dashboard/2` … `/dashboard/5` + дашборд «Источники»
(`python -m bi sources`), карточки не пустые;
повторный `python -m bi all` не создаёт дублей.
