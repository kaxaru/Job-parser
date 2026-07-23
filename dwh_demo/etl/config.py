"""Конфиг: источник данных + параметры подключения всех бэкендов. Из окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # etl/
DATA_DEFAULT = ROOT.parent.parent / "data" / "vacancies_raw.json"


@dataclass(frozen=True)
class Settings:
    data_file: Path
    pg_dsn: dict = field(default_factory=dict)
    ch_url: str = "http://localhost:8123/"
    mssql_dsn: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_file=Path(os.getenv("DATA_FILE", DATA_DEFAULT)),
            pg_dsn={
                "host": os.getenv("PGHOST", "localhost"),
                "port": int(os.getenv("PGPORT", "5433")),
                "dbname": os.getenv("PGDATABASE", "hh"),
                "user": os.getenv("PGUSER", "hh"),
                "password": os.getenv("PGPASSWORD", "hh"),
            },
            ch_url=os.getenv("CH_URL", "http://localhost:8123/"),
            mssql_dsn={
                "server": os.getenv("MSSQL_HOST", "localhost"),
                "port": int(os.getenv("MSSQL_PORT", "1433")),
                "user": os.getenv("MSSQL_USER", "sa"),
                "password": os.getenv("MSSQL_PASSWORD", "DwhDemo2026!"),
                "database": os.getenv("MSSQL_DB", "hh"),
            },
        )
