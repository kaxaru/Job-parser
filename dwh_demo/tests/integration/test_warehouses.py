"""Интеграционные тесты против ЖИВЫХ хранилищ (в CI — service containers).

ВНИМАНИЕ: грузят данные в БД `hh` с TRUNCATE — локально перезапишут демо-данные.
Гонять только в CI (одноразовые контейнеры) или против выделенной тестовой БД.
Подключение из окружения (PGHOST/CH_URL/MSSQL_HOST...), данные — из фикстуры.
Параметризуются по TARGETS — новый бэкенд покрывается автоматически.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from etl.config import Settings
from etl.pipeline import TARGETS, build_pipeline

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_vacancies.json"
EXPECTED = 5  # записей в фикстуре


@pytest.fixture
def cfg():
    # боевой DSN из окружения, но источник подменяем на маленькую фикстуру
    return replace(Settings.from_env(), data_file=FIXTURE)


@pytest.mark.parametrize("backend", TARGETS)   # postgres, clickhouse, mssql
def test_roundtrip_loads_all_records(cfg, backend):
    pl = build_pipeline([backend], cfg)
    pl.run(["all"])                       # init_schema + load
    (wh,) = pl.warehouses
    assert wh.count() == EXPECTED


def test_all_backends_agree(cfg):
    pl = build_pipeline(list(TARGETS), cfg)
    pl.run(["all"])
    counts = {wh.name: wh.count() for wh in pl.warehouses}
    assert all(c == EXPECTED for c in counts.values()), counts
