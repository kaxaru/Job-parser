# Развёртывание

Docker-стек из 12 сервисов. В отличие от родительского проекта, здесь развёртывание —
содержательная часть, а не одна команда.

## Быстрый старт

venv активирован (см. корневой [`README`](../../README.md): `python -m venv .venv3` +
активация) — поэтому ниже просто `python`:

```bash
cd dwh_demo
docker compose up -d                        # 1. поднять стек
python -m pip install -r requirements-dev.txt # 2. зависимости (один раз)
python -m etl all                           # 3. данные во все хранилища
python -m bi all                            # 4. дашборды Metabase
```

Открыть: `http://localhost:3000/dashboard/2` (`demo@hh.local` / `DwhDemo2026!`).

## Порядок и частота

Цепочка строгая — каждый шаг зависит от предыдущего:

```
docker compose up -d    инфраструктура    -> один раз (данные живут в томах)
python -m etl all       данные в БД       -> ПОВТОРЯЕМО, при каждом свежем JSON
python -m bi all        дашборды          -> один раз (или при правке дашбордов)
```

`etl` требует живых Postgres / ClickHouse / MS SQL; `bi` требует и живого Metabase,
и уже наполненных витрин — иначе карточки сошлются на пустоту.

Регулярно гоняется только `python -m etl all`. Контейнеры подняты, дашборды собраны
и сами перечитают обновлённые витрины.

## Сервисы

Порт снаружи не равен порту внутри: контейнеры ходят друг к другу по имени сервиса.
Grafana на 3001, потому что 3000 занят Metabase; MinIO на 9002, потому что 9000 занят
ClickHouse.

### `hh-postgres`

**Порт.** 5433 -> 5432 · **Внутри сети.** `hh-postgres:5432` · **Логин.** `hh` / `hh`

**Роль.** DWH PostgreSQL плюс отдельная БД `airflow` для метаданных оркестратора.

**Особенность.** БД `airflow` создаётся автоматически при первом старте скриптом из
`postgres/init/`. Grafana ходит в неё же за метриками прогонов.

**Том.** `hh-pgdata`

### `hh-clickhouse`

**Порт.** 8123 (HTTP) / 9000 (native) · **Внутри.** `hh-clickhouse:8123` ·
**Логин.** `default`, без пароля

**Роль.** DWH ClickHouse. Адаптер ходит только по HTTP, клиентская библиотека не нужна.

**Том.** `clickhouse-data`

### `hh-mssql`

**Порт.** 1433 · **Внутри.** `hh-mssql:1433` · **Логин.** `sa` / `DwhDemo2026!`

**Роль.** DWH MS SQL, источник для Power BI.

**Edge case.** Поднимается дольше остальных — 30–60 секунд. Запуск `etl -t mssql` до
состояния healthy падает; ждать.

**Том.** `mssql-data`

### `metabase`

**Порт.** 3000 · **Логин.** `demo@hh.local` / `DwhDemo2026!`

**Роль.** BI-дашборды, аналитические срезы по всем трём движкам.

**Том.** `metabase-data`

### `airflow-webserver`, `airflow-scheduler`, `airflow-init`

**Порт.** 8080 (webserver) · **Логин.** `admin` / `admin`

**Роль.** Оркестрация. `airflow-init` — одноразовая инициализация БД и пользователя.

**Особенность.** `pymssql` доставляется на буте через `_PIP_ADDITIONAL_REQUIREMENTS`,
плюс проброшен `MSSQL_HOST=hh-mssql`. После правки compose перезапускать оба сервиса.

**Том.** `airflow-logs`

### `hh-grafana`

**Порт.** 3001 · **Логин.** `admin` / `DwhDemo2026!`

**Роль.** Операционный взгляд: метрики прогонов Airflow и логи из Loki.

**Особенность.** Datasources и дашборды провижатся из `observability/` — кликать в UI
не нужно.

**Том.** `grafana-data`

### `hh-loki`

**Порт.** 3100 · **Внутри.** `loki:3100`

**Роль.** Хранилище логов. Чанки лежат в MinIO по S3-протоколу, с retention и лимитами.

**Том.** `loki-data`

### `hh-promtail`

**Порт.** нет

**Роль.** Сборщик логов всех контейнеров проекта через `docker_sd`. Парсит уровень
логирования в метку `level`.

### `hh-minio`, `minio-init`

**Порт.** 9002 (API) / 9003 (консоль) · **Внутри.** `minio:9000` ·
**Логин.** `minioadmin` / `minioadmin`

**Роль.** S3-хранилище для чанков Loki. `minio-init` одноразово создаёт бакет `loki` —
сам Loki бакет не создаёт.

**Том.** `minio-data`

## Минимальные наборы

Поднимать весь стек нужно не всегда:

- **только ETL в Postgres** — `docker compose up -d hh-postgres`
- **ETL во все хранилища** — плюс `clickhouse`, `mssql`
- **плюс BI** — плюс `metabase`
- **плюс оркестрация** — плюс `airflow-init`, `airflow-webserver`, `airflow-scheduler`
- **плюс observability** — плюс `minio`, `minio-init`, `loki`, `promtail`, `grafana`

## Остановка и сброс

```bash
docker compose down          # остановить, тома с данными сохраняются
docker compose down -v       # + удалить тома: полный сброс БД и Metabase
```

## Типичные проблемы

**Metabase: «user already exists» при `python -m bi`** — том не пуст:

```bash
docker compose rm -sf metabase && docker volume rm hh-dwh_metabase-data
docker compose up -d metabase
curl localhost:3000/api/health      # дождаться 200
python -m bi all
```

**`ModuleNotFoundError: psycopg2`** — ставить через `python -m pip install ...`:
у `.venv3\Scripts\pip.exe` шебанг указывает на другой интерпретатор.

**ClickHouse-карточки пустые** — сначала `python -m etl -t clickhouse all`.

**MS SQL не стартует, `load_mssql` падает** — ждать healthy (30–60 с). Для Airflow нужен
`pymssql` и `MSSQL_HOST=hh-mssql`; после правки compose:
`docker compose up -d airflow-scheduler airflow-webserver`.

**Loki: «entry too old» или смена схемы** — сбросить том:

```bash
docker compose rm -sf loki && docker volume rm hh-dwh_loki-data
docker compose up -d loki
```

Чанки не появляются сразу — flush по таймеру (idle 30 мин). Форсировать:
`curl -XPOST localhost:3100/flush`.

**Grafana: дашборд не обновился** — provisioning перечитывает файл за ~10 с. Либо бампнуть
`"version"` в JSON, либо `docker compose restart grafana`.

## Учётные данные

Все пароли в проекте — **демонстрационные** и лежат в `docker-compose.yml` открытым
текстом. Это осознанно: стек поднимается локально, наружу не публикуется. Для любого
не-локального развёртывания их необходимо вынести в `.env` и сменить.
