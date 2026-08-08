"""Airflow DAG: ETL вакансий HH в DWH — независимая цепочка на каждое хранилище.

Граф:  init_<t> -> load_<t> для каждого t в TARGETS
       (сейчас: postgres, clickhouse, mssql)

Цепочки между собой НЕ связаны: недоступный MS SQL валит только `init_mssql` и
`load_mssql`, Postgres и ClickHouse грузятся штатно. Это graceful degradation на
чужих сбоях (см. ../CLAUDE.md родителя). Раньше `init_schema` был ОДНОЙ таской на
все три бэкенда: `Pipeline.init_schema` идёт по адаптерам без try/except, а
`MSSQLWarehouse.init_schema` открывает настоящее соединение — поэтому не успевший
подняться MS SQL ронял init целиком, и обе оставшиеся load-таски получали
upstream_failed вместо загрузки.

Список бэкендов берётся из `etl.pipeline.TARGETS`: новый бэкенд в `REGISTRY`
появляется в графе сам, как и в CLI.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from etl.pipeline import TARGETS, build_pipeline


def init_target(target: str) -> None:
    """Схема ровно ОДНОГО хранилища — отказ движка не выходит за свою цепочку."""
    build_pipeline([target]).init_schema()


def load_target(target: str) -> None:
    """extract + transform + load ровно одного хранилища."""
    build_pipeline([target]).load()


default_args = {
    "owner": "data-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    # без потолка зависшая заливка висела бы бесконечно, а retries=1 её просто повторил
    "execution_timeout": timedelta(minutes=30),
}

with DAG(
    dag_id="hh_etl",
    description="ETL вакансий HH: JSON -> Postgres + ClickHouse + MsSql",
    default_args=default_args,
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    # по одной таске за раз. Каждая load-таска делает СВОЙ extract+transform (процессы
    # Airflow изолированы, кэш `Pipeline.prepare` живёт внутри процесса), а вход —
    # JSON на ~480 МБ; три параллельных разбора держали бы три копии графа объектов
    # в памяти. Это именно сериализация, а не зависимость: падение одной цепочки
    # по-прежнему не отменяет остальные.
    max_active_tasks=1,
    dagrun_timeout=timedelta(hours=2),
    tags=["hh", "dwh", "etl"],
) as dag:
    for t in TARGETS:
        init = PythonOperator(
            task_id=f"init_{t}", python_callable=init_target, op_kwargs={"target": t}
        )
        load = PythonOperator(
            task_id=f"load_{t}", python_callable=load_target, op_kwargs={"target": t}
        )
        init >> load
