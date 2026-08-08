# Оркестрация — Airflow

**Файл:** `dags/hh_etl_dag.py` · **DAG:** `hh_etl` · **UI:** `http://localhost:8080`
(`admin` / `admin`)

## Назначение

Тот же ETL, но по расписанию и с независимой цепочкой на каждое хранилище. Это **замена**
ручного `python -m etl`, а не дополнительный шаг: код исполняется тот же.

## Граф

```
init_postgres   ──> load_postgres
init_clickhouse ──> load_clickhouse
init_mssql      ──> load_mssql
```

Три **независимые цепочки**, между собой не связанные. Раньше `init_schema` был одной
таской на все три бэкенда, и это стоило прогонов: `Pipeline.init_schema` идёт по адаптерам
без `try/except`, а `MSSQLWarehouse.init_schema` открывает настоящее соединение — не успевший
подняться MS SQL ронял init целиком, и обе оставшиеся `load`-таски получали `upstream_failed`
вместо загрузки в два исправных хранилища. Разнесение цепочек — та же graceful degradation
на чужих сбоях, что и в родительском проекте.

**Таски строятся из `etl.pipeline.TARGETS`**, а не перечислены руками:

```python
for t in TARGETS:
    init = PythonOperator(task_id=f"init_{t}", python_callable=init_target,
                          op_kwargs={"target": t})
    load = PythonOperator(task_id=f"load_{t}", python_callable=load_target,
                          op_kwargs={"target": t})
    init >> load
```

Новый бэкенд в `REGISTRY` появляется в графе сам — это то же свойство расширяемости,
что и в CLI.

## Параметры

**Расписание.** `@daily`, `start_date=2024-01-01`, `catchup=False`.

`catchup=False` принципиально: иначе при первом включении Airflow попытался бы догнать
все пропущенные дни с 2024 года, а данные всё равно перезаливаются целиком — исторические
прогоны бессмысленны.

**Ретраи.** `retries=1`, `retry_delay=2 минуты`, `owner="data-eng"`.

**Потолки.** `execution_timeout=30 минут` на таску, `dagrun_timeout=2 часа` на прогон.
Без потолка зависшая заливка висела бы бесконечно, а `retries=1` её просто повторил.

**`max_active_tasks=1` — по одной таске за раз.** Это лечение памяти, а не зависимость
между бэкендами: каждая `load`-таска делает свой extract+transform (см. ниже), а вход —
JSON на ~480 МБ; три параллельных разбора держали бы три копии графа объектов сразу.
Цепочки от сериализации не перестают быть независимыми: падение одной по-прежнему не
отменяет остальные, они просто ждут своей очереди.

**Теги.** `hh`, `dwh`, `etl`.

## Важная деталь: transform выполняется в каждой таске

```python
def load_target(target):
    build_pipeline([target]).load()
```

Каждая `load`-таска строит свой конвейер и делает extract + transform заново. Кэш
`Pipeline.prepare()` работает внутри процесса, а процессы Airflow изолированы.

Это осознанный размен: три разбора JSON вместо одного, зато цепочки независимы и падение
одной не блокирует остальные. С `max_active_tasks=1` три разбора идут последовательно —
время прогона это не экономит, но и трёх копий распарсенного JSON в памяти не даёт.
При росте данных правильный ход — вынести transform в отдельную таску и передавать
результат через XCom или промежуточное хранилище.

## Запуск

```bash
# из UI: http://localhost:8080 -> DAG hh_etl -> включить -> Trigger

# из CLI:
docker compose exec -T airflow-scheduler airflow dags trigger hh_etl
docker compose exec -T airflow-scheduler airflow dags list-runs -d hh_etl
```

## Крайние случаи

- **`pymssql` в Airflow** — доставляется на буте через `_PIP_ADDITIONAL_REQUIREMENTS`
  в compose; без него `load_mssql` падает на импорте
- **`MSSQL_HOST=hh-mssql`** — внутри сети имя сервиса, не `localhost`
- **MS SQL не успел подняться** — падают `init_mssql` и `load_mssql`, две остальные цепочки
  проходят целиком: отказ движка не выходит за свою цепочку
- **Нет данных в `/opt/airflow/data`** — падают все три `load`: контейнерам смонтирован
  только `../data/export`, и наполняет его хост (шаг 3a крона), см.
  [`deployment.md`](deployment.md), раздел «Данные для контейнеров»
- **Деградированный вход** — санити-гейт `Pipeline._reject_degraded` роняет `load_*`, не
  тронув факт. Флага `--force` у DAG нет намеренно: решение затереть полный факт просевшим
  срезом принимают руками, `python -m etl load --force` с хоста
- **Ретрай безопасен** — load идемпотентен, полный перезалив

## Метрики прогонов

Статусы и длительности DAG-ранов лежат в БД `airflow` того же `hh-postgres` и выводятся
на дашборд Grafana «здоровье пайплайна» — см. [`observability.md`](observability.md).
Там же рядом стоят панели по самой витрине (`core.vacancies`): зелёный DAG-ран говорит,
что таски отработали, а не что данные приехали.

## Проверка

```bash
docker compose exec -T airflow-scheduler airflow dags trigger hh_etl
docker compose exec -T airflow-scheduler airflow dags list-runs -d hh_etl
```

Ожидаемо: последний ран в состоянии `success`, **шесть тасок** (`init_<t>` и `load_<t>` на
каждый из трёх бэкендов), в логе каждой `load_*` — строки вида
`[etl] [postgres] loaded: 92730` и `[etl] verify: prepare=92730, в факте {'postgres': 92730}`.
Строка `verify` — не украшение: `Pipeline._verify` сверяет факт с `prepare` и роняет таску,
называя отставший движок. Аварийные строки помечены маркером `[etl] ERROR`, по нему же их
ищут в эксплуатации — см. [`observability.md`](observability.md).
