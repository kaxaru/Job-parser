"""Адаптеры хранилищ (Ports & Adapters)."""
from .base import Warehouse
from .clickhouse import ClickHouseWarehouse
from .mssql import MSSQLWarehouse
from .postgres import PostgresWarehouse

__all__ = ["ClickHouseWarehouse", "MSSQLWarehouse", "PostgresWarehouse", "Warehouse"]
