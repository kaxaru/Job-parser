# Конфигурация

Настройки читаются **только из окружения**, дефолты подобраны под локальный
`docker compose`. Файла конфигурации нет — это осознанно: тот же код исполняется на хосте
и внутри контейнеров Airflow, где различаются лишь имена хостов.

## ETL — `etl/config.py::Settings`

`Settings.from_env()` — единственная точка сборки:

```python
data_file: Path        DATA_FILE     -> ../data/vacancies_raw.json
fx_file:   Path        FX_FILE       -> ../data/fx_rates.json
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

**`FX_FILE` (с 09.08.2026)** — суточный кеш курсов валют, который ведёт родитель
(`hrwork/infrastructure/net/rates.py::get_rates`). Стенд его только читает, в сеть за
курсами не ходит: из этих курсов считаются `salary_min_rub`/`salary_max_rub`, а на них
держатся все зарплатные витрины. Отдельная переменная, а не путь рядом с `DATA_FILE`,
потому что в контейнере Airflow каталог данных примонтирован в другое место и дефолт
`etl/config.py::FX_DEFAULT`, посчитанный от каталога пакета `etl`, там указал бы не туда.
Файла нет или он битый -> предупреждение `[etl] FX: ...`, рублёвые колонки пустые,
**прогон продолжается**: курсы — чужие данные, и деградация по ним graceful.

**`DATA_FILE` в Airflow — это ДРУГОЙ путь.** В контейнерах он равен
`/opt/airflow/data/vacancies_raw.json`, и за этим путём стоит не `../data`, а
`../data/export` (`docker-compose.yml`, `volumes` общего блока `x-airflow-common`). Рядом
с выгрузкой в `../data` лежат куки живой сессии, профиль браузера и контакты рекрутёров,
а Airflow исполняет произвольный Python — поэтому в контейнер уезжает срез, а не каталог
целиком. Расхождение двух дефолтов осознанное: локальный `FX_DEFAULT`/`DATA_DEFAULT`
ведут в `../data`, контейнерные переменные — в смонтированный `../data/export`, который
наполняет хост (шаг 3a в `cron/cron_collect.bat`). Подробности — [`deployment.md`](deployment.md).

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
должен видеть:

```python
base:           MB_URL            -> http://localhost:3000
admin_email:    MB_ADMIN_EMAIL    -> demo@hh.local
admin_password: MB_ADMIN_PASSWORD -> DwhDemo2026!
```

Этих трёх переменных в `docker-compose.yml` нет: провижининг запускается с хоста
(`python -m bi`), а не из контейнера, и ходит в Metabase на проброшенный порт.

**Читаются на импорте, а не при вызове.** `bi/config.py::Settings` — датакласс, у которого
`os.getenv` стоит в значении поля по умолчанию, поэтому окружение считывается один раз,
в момент импорта модуля. Это не то же самое, что `etl/config.py::Settings.from_env()`,
который читает окружение на каждом вызове: `os.environ["MB_URL"] = ...` после импорта
`bi.config` уже ни на что не влияет.

Имена подключений в Metabase (`PG_NAME`, `CH_NAME`, `MS_NAME`) — константы того же модуля,
а не окружение: дашборды ищут БД по имени (`find_database`), и переименование в одном месте
не должно молча ломать карточки.

## Константы, вынесенные в код

Не через окружение, потому что меняются вместе с логикой, а не со средой:

- `postgres.py::BATCH_SIZE = 1000` — строк в многострочном INSERT
- `clickhouse.py::BATCH = 2000` — строк в одном `JSONEachRow`
- `mssql.py::BATCH_SIZE = 1000` — размер `executemany`
- `domain.py::STACK_SKILL_PATTERNS` — 19 тегов общего стека, **дословная копия** части
  `hrwork/config.py::TECH_PATTERNS` (там их 55). Копия, а не импорт: `etl/` монтируется
  в контейнер Airflow без пакета `hrwork`. Посимвольное совпадение стережёт
  `tests/test_domain.py::test_stack_patterns_are_verbatim_copies_of_parent`
- `domain.py::ETL_SKILL_PATTERNS` — 18 аналитических тегов самого стенда (SQL, ETL, Airflow,
  dbt, BI-инструменты…). У родителя их нет, поэтому они считаются всегда — и поверх его
  кеша тоже; непересечение ключей стережёт `test_etl_specific_tags_do_not_shadow_parent_tags`
- `domain.py::PARENT_DETECT_SIG` — пин сигнатуры словаря стека родителя
  (`hrwork/domain/parsing.py::DETECT_SIG`). Совпала с полем `_dv` записи -> стек берётся
  из готового `_techs`, не совпала -> размечаем сами по двум словарям выше. Именно пин,
  а не вычисление: посчитать сигнатуру можно только по `hrwork.config.TECH_PATTERNS`,
  которого в контейнере нет. Протухание ловит `test_parent_detect_sig_pin_is_current`
- `domain.py::REMOTE_LIKE_CODES = ("remote", "flexible")` — что считается удалёнкой
  в витринах. Копия `hrwork/domain/schedule.py::REMOTE_LIKE_CODES`; до 09.08.2026 у стенда
  был свой, третий ответ на этот вопрос, и доля удалёнки в Metabase не сходилась с отчётами
  родителя по одной и той же выборке
- `domain.py::REMOTE_MARKERS` — текстовые признаки удалёнки. Питают **отдельное** поле
  `remote_mentioned`, а не `is_remote`: слово «удалённо» в описании — это не формат работы
- `domain.py::EXPERIENCE`, `SCHEDULE` — справочники нормализации кодов
- `rates.py::CURRENCY_ALIAS` — `RUR->RUB`, `BYR->BYN`, `USDT->USD`. Тоже копия родителя
  (`hrwork/infrastructure/net/rates.py`) под стражем `test_currency_alias_matches_parent`:
  без канонизации рубль двоится в любом фасете, потому что hh шлёт `RUR`, а getmatch `RUB`
- `pipeline.py::LOAD_MIN_RATIO`, `SANITY_MIN_ROWS`, `MAX_BROKEN_RATIO` — пороги санити-гейтов
  перезалива. В окружение не вынесены сознательно: это политика конвейера, одинаковая на всех
  стендах, тогда как `Settings` описывает параметры конкретной среды
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
