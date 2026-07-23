"""CLI и фабрики обоих пакетов (etl, bi) — без БД и HTTP.

Закрывает зоны, появившиеся после рефакторов: argparse-вход, реестр бэкендов
(TARGETS) и wiring адаптеров в build_pipeline; реестр дашбордов bi.
"""
import pytest

from etl import cli as etl_cli
from etl.pipeline import REGISTRY, TARGETS, build_pipeline
from etl.warehouse import ClickHouseWarehouse, PostgresWarehouse

pytestmark = pytest.mark.unit


# ── etl: argparse ──
def test_etl_parser_empty_is_no_steps():
    # пустой ввод -> []; нормализацию «пусто = весь конвейер» делает Pipeline.run
    assert etl_cli.build_parser().parse_args([]).steps == []


def test_etl_parser_multiple_steps():
    args = etl_cli.build_parser().parse_args(["init", "load"])
    assert args.steps == ["init", "load"]


def test_etl_parser_rejects_unknown_step():
    with pytest.raises(SystemExit):
        etl_cli.build_parser().parse_args(["frobnicate"])


def test_etl_target_and_step_coexist():
    # регресс: -t не должен жадно съедать позиционный STEP (`-t postgres load`)
    args = etl_cli.build_parser().parse_args(["-t", "postgres", "load"])
    assert args.target == ["postgres"]
    assert args.steps == ["load"]


def test_etl_target_repeatable():
    args = etl_cli.build_parser().parse_args(["-t", "postgres", "-t", "clickhouse"])
    assert args.target == ["postgres", "clickhouse"]


# ── etl: реестр бэкендов и wiring адаптеров ──
def test_targets_match_registry():
    assert set(TARGETS) == set(REGISTRY) == {"postgres", "clickhouse", "mssql"}


def test_build_pipeline_default_wires_all_adapters():
    pl = build_pipeline()                      # без аргументов = все бэкенды
    names = [w.name for w in pl.warehouses]
    assert names == ["postgres", "clickhouse", "mssql"]
    assert isinstance(pl.warehouses[0], PostgresWarehouse)
    assert isinstance(pl.warehouses[1], ClickHouseWarehouse)


def test_build_pipeline_single_backend():
    pl = build_pipeline(["clickhouse"])
    assert [w.name for w in pl.warehouses] == ["clickhouse"]


# ── bi: реестр дашбордов и argparse ──
def test_bi_registry_keys_match_class_keys():
    from bi.registry import REGISTRY as BI

    assert set(BI) == {"overview", "comparison", "cooccurrence", "mssql", "sources"}
    for key, cls in BI.items():
        assert cls.key == key            # ключ реестра == Dashboard.key


def test_all_registered_dashboards_conform_to_protocol():
    # включённый контракт: каждый зарегистрированный дашборд структурно ЯВЛЯЕТСЯ
    # Dashboard (есть key/title/build) — проверяется через @runtime_checkable Protocol
    from bi.dashboards.base import Dashboard
    from bi.registry import REGISTRY as BI

    for cls in BI.values():
        instance = cls()                              # дашборды без аргументов конструктора
        assert isinstance(instance, Dashboard), f"{cls.__name__} не соответствует Dashboard"
        assert isinstance(cls.key, str) and isinstance(cls.title, str)
        assert callable(cls.build)


def test_bi_parser_empty_is_no_dashboards():
    from bi.cli import build_parser

    assert build_parser().parse_args([]).dashboards == []


def test_bi_parser_rejects_unknown_dashboard():
    from bi.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["nonexistent"])


def test_bi_resolve_empty_and_all_mean_every_dashboard():
    from bi.cli import resolve
    from bi.registry import REGISTRY as BI

    assert resolve([]) == list(BI)           # пусто -> все
    assert resolve(["all"]) == list(BI)      # all  -> все
    assert resolve(["overview"]) == ["overview"]
