# Конфигурация

Настройки читаются **только из окружения**, дефолты подобраны под локальный
`docker compose`. Файла конфигурации нет — это осознанно: тот же код исполняется на хосте
и внутри контейнеров Airflow, где различаются лишь имена хостов.

## ETL — `etl/config.py::Settings`

`Settings.from_env()` — единственная точка сборки:

```python
data_file: Path        DATA_FILE     -> ../data/vacancies_raw.json
pg_dsn:    dict        PGHOST        -> localhost
                       PGPORT        -> 5433
                       PGDATABASE    -> hh
                       PGUSER        -> hh
                       PGPASSWORD    -> hh
ch_url:    str         CH_URL        -> http://localhost:8123/
mssql_dsn: dict        MSSQL_HOST    -> localhost
                       MSSQL_PORT    -> 1433
                       MSSQL_USER    -> sa
                       MSSQL_PASSWORD-> DwhDemo2026!
                       MSSQL_DB      -> hh
```

`DATA_FILE` по умолчанию указывает на данные родительского проекта:
`dwh_demo/../data/vacancies_raw.json`. То есть DWH питается тем, что собрал парсер.

**`PGPORT` по умолчанию 5433, а не 5432** — снаружи контейнера порт проброшен именно так,
чтобы не конфликтовать с локально установленным Postgres.

### Хост меняется в зависимости от того, откуда запускают

С хоста — `localhost`. Из контейнера Airflow — имя сервиса:

```
PGHOST=hh-postgres        CH_URL=http://hh-clickhouse:8123/     MSSQL_HOST=hh-mssql
```

Эти значения проброшены в `docker-compose.yml` для сервисов Airflow. Забытый
`MSSQL_HOST` — типовая причина падения таски `load_mssql`.

## BI — `bi/config.py`

Настройки подключения к Metabase и параметры трёх источников данных, которые Metabase
должен видеть. Учётные данные демо-стенда: `demo@hh.local` / `DwhDemo2026!`.

## Константы, вынесенные в код

Не через окружение, потому что меняются вместе с логикой, а не со средой:

- `postgres.py::BATCH_SIZE = 1000` — строк в многострочном INSERT
- `clickhouse.py::BATCH = 2000` — строк в одном `JSONEachRow`
- `mssql.py::BATCH_SIZE = 1000` — размер `executemany`
- `domain.py::SKILL_PATTERNS` — 37 навыков регэкспами
- `domain.py::EXPERIENCE`, `SCHEDULE` — справочники нормализации кодов
- `domain.py::REMOTE_MARKERS` — текстовые признаки удалёнки
- `pipeline.py::REGISTRY` / `TARGETS` — реестр бэкендов

## Линтер

`ruff.toml`, `target-version = "py310"`, `line-length = 110`.

Версия питона зафиксирована по **минимальному поддерживаемому рантайму** (`.venv3`),
а не по версии машины: иначе `pyupgrade` предложил бы синтаксис новее 3.10 и сломал
исполнение в целевом окружении.

Набор правил: `E`, `W` (pycodestyle), `F` (pyflakes), `I` (isort), `UP` (pyupgrade),
`B` (bugbear), `SIM` (simplify), `C4` (comprehensions), `RUF`.

У `dwh_demo` свой `ruff.toml` и свой `pytest.ini`, отдельные от родительского проекта —
оба инструмента выбирают ближайший к файлу конфиг.

## Зависимости

```
psycopg2-binary>=2.9    Postgres-адаптер
pymssql>=2.3            MS SQL-адаптер
```

ClickHouse-адаптер зависимостей не имеет — ходит по HTTP через stdlib `urllib`.
`requirements-dev.txt` добавляет `pytest`.

## Секреты

Все пароли демонстрационные и лежат в `docker-compose.yml` открытым текстом. Стек
локальный и наружу не публикуется.

Для любого не-локального развёртывания их необходимо вынести в `.env` и сменить —
в первую очередь `MSSQL_PASSWORD`, который сейчас продублирован и в compose,
и в дефолте `Settings.from_env`.
