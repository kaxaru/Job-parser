"""CLI и фабрики обоих пакетов (etl, bi) — без БД и HTTP.

Закрывает зоны, появившиеся после рефакторов: argparse-вход, реестр бэкендов
(TARGETS) и wiring адаптеров в build_pipeline; реестр дашбордов bi.
"""
from pathlib import Path

import pytest

from etl import cli as etl_cli
from etl import pipeline as etl_pipeline
from etl.config import Settings
from etl.pipeline import REGISTRY, TARGETS, build_pipeline
from etl.warehouse import ClickHouseWarehouse, MSSQLWarehouse, PostgresWarehouse

pytestmark = pytest.mark.unit

# Конфиг для тестов wiring: адаптеры собираются, но никуда не ходят. Файл данных не
# читается (JsonSource ленив), поэтому имя условное — важно лишь, что это НЕ дефолт
# из окружения (дефолтный DATA_FILE указывает на боевой кеш родителя ~460 МБ).
UNUSED = Settings(data_file=Path("unused.json"))

# Заголовки дашбордов. Это НЕ косметика: `bi/client.py::upsert_dashboard` ищет дашборд
# ПО ИМЕНИ, поэтому заголовок — ключ идемпотентности провижининга. Незамеченный дрейф
# создаёт ВТОРОЙ дашборд вместо обновления первого, а старый остаётся с чужими карточками.
# Перечень порталов в заголовке «источников» намеренно не пишется: их девять и список
# задаёт родитель (`hrwork/infrastructure/sources`), а заголовок обязан быть стабильным.
BI_TITLES = [
    ("overview", "HH — рынок труда (Python / Data Engineer)"),
    ("comparison", "HH — Postgres vs ClickHouse (один BI, два движка)"),
    ("cooccurrence", "HH — стек рядом с языком (co-occurrence)"),
    ("mssql", "HH — MS SQL (T-SQL DWH)"),
    ("sources", "Источники — сравнение порталов"),
]
BI_KEYS = [key for key, _ in BI_TITLES]


@pytest.fixture
def no_env(monkeypatch):
    """Растяжка на окружение: если фабрика полезет в `Settings.from_env`, тест назовёт
    это падением сам. Иначе результат зависит от того, что экспортировано в оболочке
    запускающего (`PGPORT=abc` ронял тесты wiring на разборе порта)."""
    monkeypatch.setattr(
        etl_pipeline.Settings, "from_env",
        classmethod(lambda cls: pytest.fail("build_pipeline прочитал окружение вместо cfg")))


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


@pytest.mark.parametrize("argv, force", [(["load"], False), (["load", "--force"], True)])
def test_etl_force_is_opt_in(argv, force):
    # обход санити-гейта перезалива — только явным флагом, как `hh.py collect --force`
    assert etl_cli.build_parser().parse_args(argv).force is force


# ── etl: реестр бэкендов и wiring адаптеров ──
def test_registry_lists_every_backend_in_load_order():
    # порядок — не косметика: в нём идёт fan-out и строятся таски DAG
    assert list(REGISTRY) == ["postgres", "clickhouse", "mssql"]
    assert TARGETS == ("postgres", "clickhouse", "mssql")


def test_build_pipeline_default_wires_all_adapters(no_env):
    pl = build_pipeline(cfg=UNUSED)            # без списка таргетов = все бэкенды
    assert [w.name for w in pl.warehouses] == ["postgres", "clickhouse", "mssql"]
    assert [type(w) for w in pl.warehouses] == [
        PostgresWarehouse, ClickHouseWarehouse, MSSQLWarehouse]


def test_build_pipeline_single_backend(no_env):
    pl = build_pipeline(["clickhouse"], UNUSED)
    assert [w.name for w in pl.warehouses] == ["clickhouse"]


# ── bi: реестр дашбордов и argparse ──
def test_bi_registry_holds_exactly_the_documented_dashboards():
    from bi.registry import REGISTRY as BI

    # схлопывание дублирующегося key словарь съел бы молча — ловится длиной
    assert len(BI) == 5
    assert {k: c.__name__ for k, c in BI.items()} == {
        "overview": "OverviewDashboard",
        "comparison": "ComparisonDashboard",
        "cooccurrence": "CooccurrenceDashboard",
        "mssql": "MssqlOverviewDashboard",
        "sources": "SourceComparisonDashboard",
    }


@pytest.mark.parametrize("key, title", BI_TITLES)
def test_dashboard_title_is_the_upsert_key(key, title):
    from bi.registry import REGISTRY as BI

    assert BI[key].title == title


def test_dashboard_titles_are_unique():
    # совпавшие заголовки -> второй build архивирует карточки первого дашборда
    from bi.registry import REGISTRY as BI

    assert len({c.title for c in BI.values()}) == 5


def test_bi_parser_empty_is_no_dashboards():
    from bi.cli import build_parser

    assert build_parser().parse_args([]).dashboards == []


def test_bi_parser_rejects_unknown_dashboard():
    from bi.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["nonexistent"])


@pytest.mark.parametrize("argv, expected", [
    ([], BI_KEYS),                  # пусто -> все
    (["all"], BI_KEYS),             # all   -> все
    (["overview"], ["overview"]),
])
def test_default_provisioning_set(argv, expected):
    # ожидаемое — литеральный список из docs/bi.md, а не `list(REGISTRY)`: иначе новый
    # класс в реестре автоматически попадал бы в `python -m bi` без единого красного теста
    from bi.cli import resolve

    assert resolve(argv) == expected
