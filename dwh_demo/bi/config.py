"""Конфиг BI-слоя: адрес/креды Metabase + параметры подключений к хранилищам.

Хосты — имена сервисов в сети compose (Metabase ходит к ним изнутри Docker).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    base: str = os.getenv("MB_URL", "http://localhost:3000")
    admin_email: str = os.getenv("MB_ADMIN_EMAIL", "demo@hh.local")
    admin_password: str = os.getenv("MB_ADMIN_PASSWORD", "DwhDemo2026!")


# Имена подключений в Metabase. Дашборды ищут БД по этим именам (find_database),
# поэтому они вынесены в константы — переименование здесь не сломает дашборды молча.
PG_NAME = "HH DWH"
PG_ENGINE = "postgres"
CH_NAME = "HH ClickHouse"
CH_ENGINE = "clickhouse"
MS_NAME = "HH MSSQL"
MS_ENGINE = "sqlserver"

POSTGRES = {
    "name": PG_NAME, "engine": PG_ENGINE,
    "details": {"host": "hh-postgres", "port": 5432, "dbname": "hh",
                "user": "hh", "password": "hh", "ssl": False,
                "schema-filters-type": "all"},
}

CLICKHOUSE = {
    "name": CH_NAME, "engine": CH_ENGINE,
    "details": {"host": "hh-clickhouse", "port": 8123, "user": "default",
                "password": "", "dbname": "hh", "ssl": False,
                "db-filters-type": "all", "enable-multiple-db": False},
}

MSSQL = {
    "name": MS_NAME, "engine": MS_ENGINE,
    "details": {"host": "hh-mssql", "port": 1433, "db": "hh",
                "user": "sa", "password": "DwhDemo2026!", "ssl": False,
                "schema-filters-type": "all"},
}
