"""Тесты конвейера с ФЕЙКОВЫМ Warehouse — без живой БД.

Это и есть выгода Ports & Adapters: Pipeline зависит от порта Warehouse,
в тест подставляем заглушку и проверяем оркестрацию (prepare 1×, fan-out,
санити-гейты перезалива, сверка «в факте столько же, сколько подготовлено»).
"""
from pathlib import Path

import pytest

from etl import domain
from etl.config import Settings
from etl.pipeline import Pipeline, make_warehouse
from etl.warehouse import Warehouse

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fx_rates(monkeypatch):
    """Курсы валют — внешние данные, и юниту конвейера они безразличны. Без подмены
    `domain.load_rates` читает окружение (`Settings.from_env`) и файл родителя, то есть
    результат теста зависел бы от машины запускающего."""
    monkeypatch.setattr(domain, "load_rates", lambda path=None: {"USD": 1.0, "RUB": 80.0})


class FakeWarehouse:
    """Реализует контракт Warehouse, ничего не пишет — только фиксирует вызовы.

    `rows_in_fact` — сколько строк «уже лежит» в факте до загрузки: на этом числе стоит
    санити-гейт перезалива. `calls` — журнал обращений к порту: порядок вызовов это
    наблюдаемый контракт (init до load), а не деталь реализации.
    """

    def __init__(self, name="fake", rows_in_fact=0):
        self.name = name
        self.schema_inited = 0
        self.loaded = None
        self.calls: list[str] = []
        self.rows_in_fact = rows_in_fact

    def init_schema(self):
        self.schema_inited += 1
        self.calls.append("init")

    def load(self, vacancies):
        self.loaded = list(vacancies)
        self.calls.append("load")
        self.rows_in_fact = len(self.loaded)
        return self.rows_in_fact

    def count(self):
        return self.rows_in_fact


class LosingWarehouse(FakeWarehouse):
    """Адаптер, у которого в факте оказалось МЕНЬШЕ, чем подготовлено: оборванная
    вставка, перепутанный батч, схлопнутый ключ. Порт обязан вернуть число ИЗ ХРАНИЛИЩА,
    поэтому потеря обязана быть видна конвейеру."""

    def load(self, vacancies):
        super().load(vacancies)
        self.rows_in_fact = len(vacancies) - 1
        return self.rows_in_fact


class FailingWarehouse(FakeWarehouse):
    def load(self, vacancies):
        self.calls.append("load")
        raise RuntimeError("нет связи с хранилищем")


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
# `name` списком вместо строки: домен падает на склейке текста (TypeError) — так выглядит
# смена формата поля у родителя, если она доехала до стенда
BROKEN = {"id": "b", "name": ["не строка"]}


def _raw(n, prefix="v"):
    """n сырых записей с уникальными id — вход для гейтов, считающих ОБЪЁМ."""
    return [{**VALID, "id": f"{prefix}{i}"} for i in range(n)]


# ── порт Warehouse ──
@pytest.mark.parametrize("backend", ["postgres", "clickhouse", "mssql"])
def test_registered_backend_implements_warehouse_port(backend):
    # проверяем ПРОДУКТОВЫЕ адаптеры, а не тестовый дубль: опечатку в имени метода
    # реального адаптера юниты иначе не ловят вовсе (только прогон с контейнерами)
    cfg = Settings(data_file=Path("unused.json"))
    assert isinstance(make_warehouse(backend, cfg), Warehouse)


@pytest.mark.parametrize("db_name", ["hh]; DROP DATABASE hh--", "hh'"])
def test_mssql_refuses_a_database_name_that_breaks_quoting(db_name):
    # имя БД — единственное место проекта, где значение подставляется в ТЕКСТ запроса
    # (идентификатор параметром не передать). Источник — наш конфиг, поэтому падаем сразу
    wh = make_warehouse("mssql", Settings(data_file=Path("unused.json"),
                                          mssql_dsn={"database": db_name}))
    with pytest.raises(ValueError, match="недопустимое имя базы"):
        wh.init_schema()


def test_fake_warehouse_stays_in_sync_with_the_port():
    # страж согласованности тестового дубля с портом: разойдясь, он проверял бы не тот
    # контракт (разрешённое исключение из «тестируем продуктовый код»)
    assert isinstance(FakeWarehouse(), Warehouse)


# ── prepare: отбраковка, дедуп, кэш ──
def test_prepare_drops_records_without_id():
    # id теперь строка (неймспейс по источникам), поэтому «нечисловой id» больше НЕ битый —
    # отсекается только отсутствующий/пустой id (falsy-гард), не тип
    src = FakeSource([VALID, {"name": "нет id"}, {"id": ""}])
    assert [v.id for v in Pipeline(src, []).prepare()] == ["1"]


def test_prepare_result_is_reused_without_re_reading_the_source():
    src = FakeSource([VALID])
    pl = Pipeline(src, [])
    assert [v.id for v in pl.prepare()] == ["1"]
    src.rows = [{**VALID, "id": "999"}]        # источник подменён под ногами
    assert [v.id for v in pl.prepare()] == ["1"]   # выдача та же -> перечитывания не было


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


def test_prepare_names_every_reason_a_record_was_dropped(capsys):
    # 09.08.2026: три `continue` давали один итог «стало меньше», и рост брака до размера
    # штатного дедупа (17 тыс. записей) был неотличим от нормы
    src = FakeSource([VALID, {"name": "нет id"}, {"id": ""}, BROKEN, "не объект", dict(VALID)])
    Pipeline(src, []).prepare()
    assert ("[etl] prepare: 1 вакансий (extract+transform); "
            "пропущено: no_id=2 broken=2 dup=1; "
            "FX: 4 курсов, вилка в рублях у 1\n") in capsys.readouterr().out


def test_foreign_json_with_a_wrong_type_degrades_instead_of_crashing():
    # `salary` строкой вместо объекта -> AttributeError в домене; раньше одна такая
    # запись роняла ВЕСЬ прогон (graceful degradation на чужих данных)
    src = FakeSource([VALID, {"id": "2", "salary": "нет данных"}])
    assert [v.id for v in Pipeline(src, []).prepare()] == ["1"]


@pytest.mark.parametrize("valid, broken", [
    (990, 10),      # 1 % ровно — порог не превышен, грузим
    (999, 1),       # единичный брак внешнего JSON — норма
])
def test_rare_parse_failures_are_tolerated(valid, broken):
    src = FakeSource(_raw(valid) + [{**BROKEN, "id": f"b{i}"} for i in range(broken)])
    assert len(Pipeline(src, []).prepare()) == valid


def test_mass_parse_failure_stops_the_run():
    # 09.08.2026: `except` вокруг НАШЕЙ Vacancy.from_raw глотал смену формата поля
    # на каждой записи — прогон заканчивался нулём записей и зелёной таской
    src = FakeSource(_raw(989) + [{**BROKEN, "id": f"b{i}"} for i in range(11)])
    with pytest.raises(RuntimeError, match="prepare: битых записей 11 из 1000"):
        Pipeline(src, []).prepare()


# ── run: порядок шагов ──
def test_init_schema_hits_all_warehouses():
    w1, w2 = FakeWarehouse("a"), FakeWarehouse("b")
    Pipeline(FakeSource([]), [w1, w2]).run(["init"])
    assert (w1.schema_inited, w2.schema_inited) == (1, 1)


def test_load_fans_out_to_every_warehouse_once():
    src = FakeSource([VALID])
    w1, w2 = FakeWarehouse("a"), FakeWarehouse("b")
    Pipeline(src, [w1, w2]).run(["load"])
    assert [len(w1.loaded), len(w2.loaded)] == [1, 1]
    assert w1.loaded[0].id == "1"
    assert src.reads == 1          # один extract+transform на оба бэкенда


def test_all_runs_init_then_load():
    # порядок вызовов ПОРТА — наблюдаемый контракт: load до init на чистой БД падает
    # («staging.stg_vacancies не существует»), а счётчики вызовов перестановку не видят
    w = FakeWarehouse()
    Pipeline(FakeSource([VALID]), [w]).run(["all"])
    assert w.calls == ["init", "load"]


def test_empty_steps_means_full_pipeline():
    # `python -m etl` без аргументов -> [] -> весь конвейер
    w = FakeWarehouse()
    Pipeline(FakeSource([VALID]), [w]).run([])
    assert w.calls == ["init", "load"]


# ── санити-гейт перезалива ──
def test_empty_input_is_refused_before_any_warehouse_is_touched():
    # 09.08.2026: пустой вход молча делал TRUNCATE трёх хранилищ, вставку нуля строк
    # и завершался успехом — «loaded: 0» выглядело как нормальный прогон
    w = FakeWarehouse("postgres", rows_in_fact=92730)
    with pytest.raises(RuntimeError, match="нечего грузить: prepare вернул 0 вакансий"):
        Pipeline(FakeSource([]), [w]).load()
    assert w.calls == []
    assert w.count() == 92730


@pytest.mark.parametrize("rows_in_fact, incoming", [
    (92730, 1000),      # обвал источника: 1 % от вчерашнего факта
    (1000, 499),        # на волос ниже половины (порог 50 %, как COLLECT_MIN_RATIO)
])
def test_degraded_input_does_not_overwrite_the_fact(rows_in_fact, incoming):
    w = FakeWarehouse("postgres", rows_in_fact=rows_in_fact)
    with pytest.raises(RuntimeError, match=f"деградированный вход: postgres: {incoming} << {rows_in_fact}"):
        Pipeline(FakeSource(_raw(incoming)), [w]).load()
    assert w.calls == []
    assert w.count() == rows_in_fact      # вчерашний срез на месте


@pytest.mark.parametrize("rows_in_fact, incoming", [
    (1000, 500),        # ровно половина — граница проходит
    (1000, 2000),       # рынок вырос
    (499, 1),           # факт меньше 500 строк: доли не показательны, гейт молчит
    (0, 1),             # первый прогон на пустом хранилище
])
def test_normal_slice_passes_the_gate(rows_in_fact, incoming):
    w = FakeWarehouse("postgres", rows_in_fact=rows_in_fact)
    Pipeline(FakeSource(_raw(incoming)), [w]).load()
    assert w.calls == ["load"]
    assert w.count() == incoming


def test_gate_checks_every_warehouse_before_loading_the_first():
    # иначе postgres успел бы затереть свой факт раньше, чем гейт скажет «нет» про mssql
    ok = FakeWarehouse("postgres", rows_in_fact=0)
    slumped = FakeWarehouse("mssql", rows_in_fact=92730)
    with pytest.raises(RuntimeError, match="деградированный вход: mssql: 1000 << 92730"):
        Pipeline(FakeSource(_raw(1000)), [ok, slumped]).load()
    assert ok.calls == []


def test_force_loads_a_degraded_slice():
    # реальный спад рынка/сужение фильтров — решение человека, как `hh.py collect --force`
    w = FakeWarehouse("postgres", rows_in_fact=92730)
    Pipeline(FakeSource(_raw(1000)), [w]).run(["load"], force=True)
    assert w.calls == ["load"]
    assert w.count() == 1000


def test_gate_is_skipped_when_the_previous_volume_is_unreadable(capsys):
    # `load` без `init` на свежей БД: count() падает, но гейт не должен быть строже
    # самой загрузки — она упадёт сама и внятнее
    class NoSchema(FakeWarehouse):
        def count(self):
            if not self.calls:
                raise RuntimeError('relation "core.vacancies" does not exist')
            return self.rows_in_fact

    w = NoSchema("postgres")
    Pipeline(FakeSource(_raw(1000)), [w]).load()
    assert w.calls == ["load"]
    assert "[etl] [postgres] прошлый объём недоступен" in capsys.readouterr().out


# ── сверка результата ──
def test_backend_that_lost_rows_fails_the_run():
    # 09.08.2026: ClickHouse возвращал число ОТПРАВЛЕННЫХ строк, поэтому «loaded: N
    # одинаково у трёх движков» для него не могло провалиться по построению
    w = LosingWarehouse("clickhouse")
    with pytest.raises(RuntimeError, match="расхождение факта с prepare=2"):
        Pipeline(FakeSource([VALID, {**VALID, "id": "2"}]), [w]).load()


def test_partial_fanout_names_the_already_reloaded_backends(capsys):
    ok = FakeWarehouse("postgres")
    dead = FailingWarehouse("clickhouse")
    with pytest.raises(RuntimeError, match="нет связи с хранилищем"):
        Pipeline(FakeSource([VALID]), [ok, dead]).load()
    assert ("[etl] ERROR [clickhouse] загрузка упала; уже перезалиты: ['postgres']"
            in capsys.readouterr().err)


def test_successful_load_logs_the_counts_of_every_backend(capsys):
    # строка, по которой расхождение движков видно В ЭКСПЛУАТАЦИИ, а не глазом
    # по трём отдельным `loaded:` в разных местах лога
    pl = Pipeline(FakeSource([VALID]), [FakeWarehouse("postgres"), FakeWarehouse("mssql")])
    pl.load()
    assert ("[etl] verify: prepare=1, в факте {'postgres': 1, 'mssql': 1}\n"
            in capsys.readouterr().out)
