# Развёртывание

Docker-стек из 12 сервисов. В отличие от родительского проекта, здесь развёртывание —
содержательная часть, а не одна команда.

## Быстрый старт

venv активирован (см. корневой [`README`](../../README.md): `python -m venv .venv3` +
активация) — поэтому ниже просто `python`:

```bash
cd dwh_demo
# 0. наполнить ../data/export — ДО `up`, если нужен Airflow (см. следующий раздел)
docker compose up -d                        # 1. поднять стек
python -m pip install -r requirements-dev.txt # 2. зависимости (один раз)
python -m etl all                           # 3. данные во все хранилища
python -m bi all                            # 4. дашборды Metabase
```

Открыть: `http://localhost:3000/dashboard/2` (`demo@hh.local` / `DwhDemo2026!`).

## Данные для контейнеров: `data/export`

Шаги 3 и 4 идут **с хоста** и этого каталога не требуют: `python -m etl` читает
`../data/vacancies_raw.json` и `../data/fx_rates.json` напрямую (`etl/config.py::DATA_DEFAULT`,
`FX_DEFAULT`). Каталог нужен **контейнерам Airflow** — им смонтирован только он:

```bat
:: из корня репозитория, до `docker compose up -d`
mkdir data\export
copy /y data\vacancies_raw.json data\export\vacancies_raw.json.tmp
move /y data\export\vacancies_raw.json.tmp data\export\vacancies_raw.json
copy /y data\fx_rates.json      data\export\fx_rates.json.tmp
move /y data\export\fx_rates.json.tmp      data\export\fx_rates.json
```

Ровно это делает шаг **3a** в [`cron/cron_collect.bat`](../../cron/cron_collect.bat) после
каждого сбора — руками копировать нужно только до первого запуска.

**Каталога нет -> Docker молча создаст пустой**, и таска `load_*` упадёт на отсутствующем
файле. Падение здесь лучше пустой загрузки, но выглядит как поломка ETL, хотя причина —
незаполненная выгрузка.

**Почему каталог, а не два файла.** Bind одного файла прибивает inode: родитель обновляет
выгрузку атомарно (запись во временное имя + переименование), хост после этого видит новый
файл, а контейнер навсегда остаётся на первом снимке. Bind каталога такого эффекта не даёт.

**Почему не весь `../data`.** Рядом с выгрузкой лежат `hh_state.json` (куки живой сессии HH),
`browser_profile/` (persistent-профиль Chromium), `chat_messages.json` и
`talanto_contacts.json` (телефоны и телеграмы рекрутёров — чужие ПДн). Airflow исполняет
произвольный Python, то есть всё перечисленное было доступно и коду DAG, и любому, кто дошёл
до UI на 8080. Каталог сужен 09.08.2026 тем же приёмом, каким 08.08.2026 сузили раздачу
статики у родителя: не «весь каталог», а allowlist.

**Копия атомарная** (tmp + move, как в шаге 3a): контейнер читает каталог во время работы,
и половина 480-мегабайтного файла в нём хуже вчерашнего файла целиком.

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
Grafana на 3001, потому что 3000 занят Metabase.

**Все восемь пробросов привязаны к `127.0.0.1`.** Короткая форма (`"5433:5432"`) слушала бы
на всех интерфейсах, а пароли лежат в `docker-compose.yml` открытым текстом — «наружу не
публикуется» должно быть свойством файла, а не намерением. Два порта не публикуются вовсе:
S3-API MinIO и native-протокол ClickHouse (оба 9000) нужны только соседним контейнерам
внутри сети compose.

Долгоживущие сервисы объявлены `restart: unless-stopped` и переживают перезагрузку хоста;
одноразовые `airflow-init` и `minio-init` перекрывают это своим `restart: "no"`.

### `hh-postgres`

**Порт.** 5433 -> 5432 · **Внутри сети.** `hh-postgres:5432` · **Логин.** `hh` / `hh`

**Роль.** DWH PostgreSQL плюс отдельная БД `airflow` для метаданных оркестратора.

**Особенность.** БД `airflow` создаётся автоматически при первом старте скриптом из
`postgres/init/`. Grafana ходит в неё же за метриками прогонов.

**Том.** `hh-pgdata`

### `hh-clickhouse`

**Порт.** 8123 (HTTP) · **Внутри.** `hh-clickhouse:8123` · **Логин.** `default`, без пароля

**Роль.** DWH ClickHouse. Адаптер ходит только по HTTP, клиентская библиотека не нужна.

**Особенность.** Native TCP 9000 наружу не публикуется совсем: у `default` нет пароля, а
через табличные функции `file()`/`url()` открытый порт означает чтение файлов контейнера кем
угодно. Консольный клиент запускается внутри контейнера:
`docker compose exec clickhouse clickhouse-client --database hh`.

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

**Данные.** Смонтирован только `../data/export` (`:ro`) -> `/opt/airflow/data`; чем его
наполнять — см. раздел «Данные для контейнеров». `DATA_FILE=/opt/airflow/data/vacancies_raw.json`
и `FX_FILE=/opt/airflow/data/fx_rates.json` заданы явно: дефолты из `etl/config.py` считаются
от каталога пакета и в контейнере указали бы не туда.

**Без `FX_FILE` (или без самого файла) прогон НЕ падает** — `salary_min_rub` /
`salary_max_rub` приедут пустыми, а зарплатные витрины окажутся пустыми тоже. Деградация
осознанная: курсы — чужие данные, и `etl/rates.py` не имеет права ронять прогон из-за них.
В логе это видно строкой `[etl] FX: нет кеша курсов ...`.

**Особенность.** `pymssql` доставляется на буте через `_PIP_ADDITIONAL_REQUIREMENTS`,
плюс проброшен `MSSQL_HOST=hh-mssql`. После правки compose перезапускать оба сервиса.

**Живучесть.** `restart: unless-stopped` у webserver и scheduler; у планировщика есть
healthcheck (`airflow jobs check --job-type SchedulerJob`) — без него мёртвый scheduler
выглядит в `docker compose ps` живым: контейнер поднят, DAG-раны не создаются, и узнать об
этом было неоткуда.

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

**Порт.** 9003 -> 9001 (web-консоль) · **Внутри.** `minio:9000` (S3 API) ·
**Логин.** `minioadmin` / `minioadmin`

**Роль.** S3-хранилище для чанков Loki. `minio-init` одноразово создаёт бакет `loki` —
сам Loki бакет не создаёт.

**Особенность.** S3-API наружу не публикуется: по нему ходит только Loki внутри сети
compose. Снаружи нужна одна консоль.

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

**Airflow: `load_*` падает на «нет файла `/opt/airflow/data/vacancies_raw.json`»** — не
наполнен `data/export` на хосте, Docker создал каталог пустым. Лечится разделом «Данные для
контейнеров»; на хосте то же самое делает шаг 3a крона.

**Рублёвые колонки и зарплатные витрины пустые, прогон зелёный** — нет кеша курсов
(`data/export/fx_rates.json` в контейнере, `data/fx_rates.json` на хосте) либо не задан
`FX_FILE`. Ищется строкой `[etl] FX:` в логе прогона.

**Стенд сломался «сам по себе», без правок в репозитории** — у четырёх образов теги не
закреплены: `metabase/metabase:latest`, `minio/minio:latest`, `minio/mc:latest`,
`mcr.microsoft.com/mssql/server:2022-latest`. Что поднимется, зависит от даты `docker compose
pull`. Компромисс осознанный и подписан комментарием у каждого из четырёх образов: точный тег
(`metabase/metabase:v0.NN.M`, `2022-CU<N>-ubuntu-22.04`, `RELEASE.YYYY-MM-DDTHH-MM-SSZ`)
нельзя придумать, его берут из реестра, а выдуманный номер ломает `pull` жёстче, чем
плавающий latest. Закрепление отложено до сверки с реестром. Дороже всего это стоит у
Metabase: app-db в томе `metabase-data` после апгрейда не откатывается на младшую версию, и
«починка» сводится к `docker volume rm hh-dwh_metabase-data`, то есть к потере всех карточек.

**Loki: «entry too old» или смена схемы** — сбросить том:

```bash
docker compose rm -sf loki && docker volume rm hh-dwh_loki-data
docker compose up -d loki
```

Чанки не появляются сразу — flush по таймеру (idle 30 мин). Форсировать:
`curl -XPOST localhost:3100/flush`. Частая прежде причина «too old» — рестарт Promtail — с
09.08.2026 отпала: позиции чтения лежат в томе `promtail-pos`.

**Grafana: дашборд не обновился** — provisioning перечитывает файл за ~10 с. Либо бампнуть
`"version"` в JSON, либо `docker compose restart grafana`.

## Учётные данные

Все пароли в проекте — **демонстрационные** и лежат в `docker-compose.yml` открытым
текстом. Это осознанно: стек поднимается локально, наружу не публикуется — и с 09.08.2026
это обеспечено файлом, а не обещанием: каждый проброс порта начинается с `127.0.0.1`.
Для любого не-локального развёртывания пароли необходимо вынести в `.env` и сменить,
а привязку к петле — пересмотреть осознанно, а не потерять при копировании.
