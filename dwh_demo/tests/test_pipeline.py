"""Тесты конвейера с ФЕЙКОВЫМ Warehouse — без живой БД.

Это и есть выгода Ports & Adapters: Pipeline зависит от порта Warehouse,
в тест подставляем заглушку и проверяем оркестрацию (prepare 1×, fan-out, skip).
"""
import pytest

from etl.pipeline import Pipeline
from etl.warehouse import Warehouse

pytestmark = pytest.mark.unit


class FakeWarehouse:
    """Реализует контракт Warehouse, ничего не пишет — только фиксирует вызовы."""

    def __init__(self, name="fake"):
        self.name = name
        self.schema_inited = 0
        self.loaded = None

    def init_schema(self):
        self.schema_inited += 1

    def load(self, vacancies):
        self.loaded = list(vacancies)
        return len(self.loaded)

    def count(self):
        return len(self.loaded or [])


class FakeSource:
    def __init__(self, rows):
        self.rows = rows
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.rows


VALID = {
    "id": "1", "name": "Python dev", "area": {"name": "Москва"},
    "salary": {"from": 100000, "to": None, "currency": "RUR", "gross": False},
    "experience": {"id": "between1And3"}, "schedule": {"id": "remote"},
    "employer": {"name": "X"}, "snippet": {"requirement": "Python FastAPI"},
    "description_html": "", "_city": "Москва", "_query": "q",
}


def test_fake_satisfies_warehouse_protocol():
    assert isinstance(FakeWarehouse(), Warehouse)


def test_prepare_caches_and_skips_bad():
    # id теперь строка (неймспейс по источникам), поэтому «нечисловой id» больше НЕ битый —
    # отсекается только отсутствующий/пустой id (falsy-гард), не тип
    src = FakeSource([VALID, {"name": "нет id"}, {"id": ""}])   # оба без валидного id
    pl = Pipeline(src, [])
    recs = pl.prepare()
    assert len(recs) == 1          # битые пропущены
    pl.prepare()                   # повторный вызов
    assert src.reads == 1          # extract+transform закэширован


def test_prepare_keeps_namespaced_string_ids():
    # id из hirify/talanto — строки с префиксом; раньше int() ронял их в except -> терялись
    src = FakeSource([{**VALID, "id": "talanto_e9f", "_source": "talanto"},
                      {**VALID, "id": "hirify_733", "_source": "hirify"}])
    recs = Pipeline(src, []).prepare()
    assert [(v.id, v.source) for v in recs] == [("talanto_e9f", "talanto"), ("hirify_733", "hirify")]


def test_prepare_dedups_by_id():
    # одна вакансия из двух поисковых запросов: без дедупа PG/MSSQL падают
    # на PRIMARY KEY(id), ClickHouse молча задваивает счётчики
    dup = {**VALID, "_query": "другой запрос"}
    src = FakeSource([VALID, dup, {**VALID, "id": "2"}])
    recs = Pipeline(src, []).prepare()
    assert [v.id for v in recs] == ["1", "2"]


def test_init_schema_hits_all_warehouses():
    w1, w2 = FakeWarehouse("a"), FakeWarehouse("b")
    Pipeline(FakeSource([]), [w1, w2]).run(["init"])
    assert w1.schema_inited == 1 and w2.schema_inited == 1


def test_load_fans_out_to_every_warehouse_once():
    src = FakeSource([VALID])
    w1, w2 = FakeWarehouse("a"), FakeWarehouse("b")
    Pipeline(src, [w1, w2]).run(["load"])
    assert [len(w1.loaded), len(w2.loaded)] == [1, 1]
    assert w1.loaded[0].id == "1"
    assert src.reads == 1          # один extract+transform на оба бэкенда


def test_all_runs_init_then_load():
    w = FakeWarehouse()
    Pipeline(FakeSource([VALID]), [w]).run(["all"])
    assert w.schema_inited == 1
    assert w.count() == 1


def test_empty_steps_means_full_pipeline():
    # `python -m etl` без аргументов -> [] -> весь конвейер
    w = FakeWarehouse()
    Pipeline(FakeSource([VALID]), [w]).run([])
    assert w.schema_inited == 1 and w.count() == 1
