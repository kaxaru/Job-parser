"""Airflow DAG: ETL вакансий HH в DWH с fan-out по хранилищам.

Граф:  init_schema → [load_<t> для каждого t в TARGETS]
       (сейчас: load_postgres, load_clickhouse, load_mssql)
init создаёт схемы во всех бэкендах; затем каждая load-таска независимо
грузит свой бэкенд (extract+transform выполняются внутри каждой таски —
процессы Airflow изолированы). Список бэкендов берётся из etl.pipeline.TARGETS.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from etl.pipeline import TARGETS, build_pipeline


def init_all():
    build_pipeline(TARGETS).init_schema()


def load_target(target):
    build_pipeline([target]).load()


default_args = {"owner": "data-eng", "retries": 1, "retry_delay": timedelta(minutes=2)}

with DAG(
    dag_id="hh_etl",
    description="ETL вакансий HH: JSON -> Postgres + ClickHouse + MsSql",
    default_args=default_args,
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["hh", "dwh", "etl"],
) as dag:
    init = PythonOperator(task_id="init_schema", python_callable=init_all)
    loads = [
        PythonOperator(task_id=f"load_{t}", python_callable=load_target, op_kwargs={"target": t})
        for t in TARGETS
    ]
    init >> loads
