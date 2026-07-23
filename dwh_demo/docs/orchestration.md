# Оркестрация — Airflow

**Файл:** `dags/hh_etl_dag.py` · **DAG:** `hh_etl` · **UI:** `http://localhost:8080`
(`admin` / `admin`)

## Назначение

Тот же ETL, но по расписанию и с параллельной загрузкой в хранилища. Это **замена**
ручного `python -m etl`, а не дополнительный шаг: код исполняется тот же.

## Граф

```
init_schema ──┬──> load_postgres
              ├──> load_clickhouse
              └──> load_mssql
```

Fan-out: `init_schema` создаёт схемы во всех бэкендах, затем каждая `load`-таска
независимо грузит свой.

**Таски строятся из `etl.pipeline.TARGETS`**, а не перечислены руками:

```python
loads = [
    PythonOperator(task_id=f"load_{t}", python_callable=load_target, op_kwargs={"target": t})
    for t in TARGETS
]
init >> loads
```

Новый бэкенд в `REGISTRY` появляется в графе сам — это то же свойство расширяемости,
что и в CLI.

## Параметры

**Расписание.** `@daily`, `start_date=2024-01-01`, `catchup=False`.

`catchup=False` принципиально: иначе при первом включении Airflow попытался бы догнать
все пропущенные дни с 2024 года, а данные всё равно перезаливаются целиком — исторические
прогоны бессмысленны.

**Ретраи.** `retries=1`, `retry_delay=2 минуты`, `owner="data-eng"`.

**Теги.** `hh`, `dwh`, `etl`.

## Важная деталь: transform выполняется в каждой таске

```python
def load_target(target):
    build_pipeline([target]).load()
```

Каждая `load`-таска строит свой конвейер и делает extract + transform заново. Кэш
`Pipeline.prepare()` работает внутри процесса, а процессы Airflow изолированы.

Это осознанный размен: три разбора JSON вместо одного, зато таски независимы и падение
одной не блокирует остальные. При росте данных правильный ход — вынести transform в
отдельную таску и передавать результат через XCom или промежуточное хранилище.

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
- **MS SQL не успел подняться** — `load_mssql` падает, `load_postgres` и `load_clickhouse`
  проходят: fan-out изолирует отказ одного хранилища
- **Ретрай безопасен** — load идемпотентен, полный перезалив

## Метрики прогонов

Статусы и длительности DAG-ранов лежат в БД `airflow` того же `hh-postgres` и выводятся
на дашборд Grafana «здоровье пайплайна» — см. [`observability.md`](observability.md).

## Проверка

```bash
docker compose exec -T airflow-scheduler airflow dags trigger hh_etl
docker compose exec -T airflow-scheduler airflow dags list-runs -d hh_etl
```

Ожидаемо: последний ран в состоянии `success`, четыре таски (`init_schema` плюс три
`load_*`), в логах каждой — строки вида `[etl] [postgres] loaded: 9831`.
