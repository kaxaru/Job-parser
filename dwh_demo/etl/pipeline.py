"""Конвейер ETL: extract+transform делаются ОДИН раз (в Python), затем результат
грузится в каждое инжектированное хранилище. Бэкенд — внешняя зависимость (DI)."""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .domain import Vacancy
from .source import JsonSource
from .warehouse import ClickHouseWarehouse, MSSQLWarehouse, PostgresWarehouse, Warehouse

SQL_DIR = Path(__file__).resolve().parent / "sql"

# реестр доступных бэкендов: имя -> как собрать адаптер из настроек
REGISTRY = {
    "postgres": lambda cfg: PostgresWarehouse(cfg.pg_dsn, SQL_DIR / "postgres" / "schema.sql"),
    "clickhouse": lambda cfg: ClickHouseWarehouse(cfg.ch_url, SQL_DIR / "clickhouse" / "schema.sql"),
    "mssql": lambda cfg: MSSQLWarehouse(cfg.mssql_dsn, SQL_DIR / "mssql" / "schema.sql"),
}
TARGETS = tuple(REGISTRY)


def log(msg: str) -> None:
    print(f"[etl] {msg}", flush=True)


class Pipeline:
    def __init__(self, source: JsonSource, warehouses: list[Warehouse]):
        self.source = source
        self.warehouses = warehouses
        self._records: list[Vacancy] | None = None

    def prepare(self) -> list[Vacancy]:
        """extract + transform (один раз, кэшируется)."""
        if self._records is None:
            recs = []
            # одна вакансия приходит из нескольких поисковых запросов (_query) —
            # дубль по id уронит PG/MSSQL (PRIMARY KEY) и задвоит счётчики CH
            seen: set[str] = set()
            for r in self.source.read():
                if not r.get("id"):
                    continue
                try:
                    v = Vacancy.from_raw(r)
                except (KeyError, ValueError, TypeError):
                    continue
                if v.id in seen:
                    continue
                seen.add(v.id)
                recs.append(v)
            self._records = recs
            log(f"prepare: {len(recs)} вакансий (extract+transform)")
        return self._records

    def init_schema(self) -> None:
        for wh in self.warehouses:
            wh.init_schema()
            log(f"[{wh.name}] schema ready")

    def load(self) -> None:
        records = self.prepare()
        for wh in self.warehouses:
            n = wh.load(records)
            log(f"[{wh.name}] loaded: {n}")

    def run(self, steps) -> None:
        if not steps or "all" in steps:
            steps = ["init", "load"]
        if "init" in steps:
            self.init_schema()
        if "load" in steps:
            self.load()


def make_warehouse(name: str, cfg: Settings) -> Warehouse:
    return REGISTRY[name](cfg)


def build_pipeline(targets=TARGETS, cfg: Settings | None = None) -> Pipeline:
    """Фабрика: собирает конвейер с источником и адаптерами выбранных бэкендов."""
    cfg = cfg or Settings.from_env()
    warehouses = [make_warehouse(t, cfg) for t in targets]
    return Pipeline(JsonSource(cfg.data_file), warehouses)
