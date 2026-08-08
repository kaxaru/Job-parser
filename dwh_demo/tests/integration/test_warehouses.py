"""Интеграционные тесты против ЖИВЫХ хранилищ (в CI — service containers).

ВНИМАНИЕ: грузят данные в БД `hh` с TRUNCATE — локально перезапишут демо-данные.
Гонять только в CI (одноразовые контейнеры) или против выделенной тестовой БД.
Подключение из окружения (PGHOST/CH_URL/MSSQL_HOST...), данные — из фикстуры.
Параметризуются по TARGETS — новый бэкенд покрывается автоматически.

`fixtures/sample_vacancies.json` — по одной записи на КЛАСС боевых данных (раньше было
пять круглых id "1".."5", и на них сходилось всё что угодно). Каждая запись несёт все 19
ключей сырой схемы родителя (`hrwork/infrastructure/storage/repository.py::_to_dict`):

  1. `121000001` — hh, вилка в рублях, свежий кеш стека родителя (`_dv` совпал с пином);
  2. `121000002` — hh, протухший `_dv`: стек пересчитывается словарём стенда;
  3. `121000003` — hh, зарплата null, график `flexible` (гибрид — тоже удалёнкоподобность);
  4. `getmatch_54219` — второй портал, вилка уже net;
  5. `hirify_733072` — валюта BYR (канонизируется в BYN), опыт `noExperience`;
  6. `arbeitnow_…-253251` — id-слаг 108 симв., ПУСТЫЕ работодатель/опыт/график/город,
     вилка в EUR;
  7. `arbeitnow_…-253252` — соседняя вакансия того же объявления: первые 80 символов id
     СОВПАДАЮТ с записью 6, различаются последние. Класс живой (arbeitnow плодит слаги
     локацией), и он же — детектор молчаливого усечения ключа: схлопнутся в одну строку
     -> `count()` вернёт 8 вместо 9, а не «просто загрузилось»;
  8. `himalayas_…-1284673` — id-слаг 144 симв. (максимум в срезе 09.08.2026: 732 id
     длиннее 80 симв.), вилка в USD, город «Remote»;
  9. `talanto_<uuid>` — город-агрегатор длиной 202 симв.: домен режет его до `CITY_MAX`,
     иначе не влезает в MSSQL NVARCHAR(450)/UNIQUE-индекс `core.cities`.

Классы 6–8 держат стенд честным: до 09.08.2026 фикстура состояла из id длиной один символ,
и таргет mssql (`id NVARCHAR(80)`) проходил тест зелёным, не будучи способен загрузить
боевые данные вовсе.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from etl import rates
from etl.config import Settings
from etl.pipeline import TARGETS, build_pipeline

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "sample_vacancies.json"
FX_FIXTURE = FIXTURES / "fx_rates.json"
EXPECTED = 9  # записей в фикстуре: по одной на класс боевых данных


@pytest.fixture
def cfg(monkeypatch):
    """Боевой DSN из окружения, но источник данных и курсы валют — фикстуры.

    Курсы пинятся файлом, а не берутся из суточного кеша родителя (`data/fx_rates.json`):
    в фикстуре есть вилки в EUR/USD/BYN, и на живых курсах «числа сошлись» означало бы
    в понедельник не то же, что в пятницу, а в CI кеша нет вовсе. Значения в
    `fixtures/fx_rates.json` синтетические и круглые (USD 1.0, RUB 100.0, EUR 0.5,
    BYN 2.5) — как в `tests/test_domain.py::FX`, чтобы рублёвые суммы можно было писать
    литералом. `reset_cache` — потому что `rates.load_rates` читает файл один раз
    на процесс."""
    monkeypatch.setenv("FX_FILE", str(FX_FIXTURE))
    rates.reset_cache()
    yield replace(Settings.from_env(), data_file=FIXTURE, fx_file=FX_FIXTURE)
    rates.reset_cache()


@pytest.mark.parametrize("backend", TARGETS)   # postgres, clickhouse, mssql
def test_roundtrip_loads_all_records(cfg, backend):
    pl = build_pipeline([backend], cfg)
    pl.run(["all"])                       # init_schema + load
    (wh,) = pl.warehouses
    assert wh.count() == EXPECTED


def test_all_backends_agree(cfg):
    # 09.08.2026: было `assert all(c == EXPECTED for c in counts.values())` — утверждение
    # ВАКУУМНО истинно на пустом словаре. Выпади бэкенд из REGISTRY, «все движки сошлись»
    # осталось бы зелёным на двух движках и даже на нуле. Литеральный словарь целиком
    # фиксирует и состав движков, и число строк, и называет виновника расхождения.
    pl = build_pipeline(list(TARGETS), cfg)
    pl.run(["all"])
    counts = {wh.name: wh.count() for wh in pl.warehouses}
    assert counts == {"postgres": 9, "clickhouse": 9, "mssql": 9}
