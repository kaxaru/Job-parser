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
  5. `hirify_733072` — валюта BYR (канонизируется в BYN), опыт `noExperience`, график
     `shift`: код, которого нет в справочнике `domain.py::SCHEDULE` (домен родителя его не
     производит), — проверяет контракт «незнакомый НЕпустой код проходит как есть»;
  6. `arbeitnow_…-253251` — id-слаг 108 симв., ПУСТЫЕ работодатель/опыт/график/город,
     вилка в EUR;
  7. `arbeitnow_…-253252` — соседняя вакансия того же объявления: первые 80 символов id
     СОВПАДАЮТ с записью 6, различаются последние. Класс живой (arbeitnow плодит слаги
     локацией), и он же — детектор молчаливого усечения ключа: схлопнутся в одну строку
     -> `count()` вернёт 8 вместо 9, а не «просто загрузилось»;
  8. `himalayas_…-1284673` — id-слаг 144 симв. (максимум в срезе 09.08.2026: 732 id
     длиннее 80 симв.), вилка в USD, локация — сентинел «Remote» (в измерение НЕ попадает,
     `domain.py::_location`);
  9. `talanto_<uuid>` — город-агрегатор длиной 202 симв.: домен режет его до `CITY_MAX`,
     иначе не влезает в MSSQL NVARCHAR(450)/UNIQUE-индекс `core.cities`.

Классы 6–8 держат стенд честным: до 09.08.2026 фикстура состояла из id длиной один символ,
и таргет mssql (`id NVARCHAR(80)`) проходил тест зелёным, не будучи способен загрузить
боевые данные вовсе.
"""
import json
import urllib.request
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

# Слаг-id и город-агрегатор выписаны ЦЕЛИКОМ, а не сокращены: их длина (108, 144 и 200
# символов) и есть предмет проверки — ширина ключа в MS SQL и кап `domain.py::CITY_MAX`.
# Сокращённый литерал прошёл бы там, где боевая строка не влезает.
ID_ARBEITNOW_A = ("arbeitnow_payroll-analyst-sony-business-europe-viables-industrial-estate-jays-cl"
                  "-basingstoke-rg22-4sb-253251")
ID_ARBEITNOW_B = ("arbeitnow_payroll-analyst-sony-business-europe-viables-industrial-estate-jays-cl"
                  "-basingstoke-rg22-4sb-253252")
ID_HIMALAYAS = ("himalayas_gitlab-senior-backend-engineer-distributed-systems-and-mentoring-remot"
                "e-europe-middle-east-and-africa-emea-full-time-permanent-1284673")
# 202 символа исходной строки, обрезанные доменом до CITY_MAX = 200 (обрыв внутри слова
# «Rico» — так и должно быть: кап режет по символам, а не по границе списка)
CITY_AGGREGATOR = ("Remote, Argentina, Bolivia, Brazil, Chile, Colombia, Costa Rica, Ecuador, El"
                   " Salvador, Guatemala, Honduras, Mexico, Nicaragua, Panama, Paraguay, Peru, U"
                   "ruguay, Venezuela, Dominican Republic, Puerto Ri")

#: Колонки факта, которые сверяются по значению. Взяты не все: сюда попали ровно те пары
#: соседей из `warehouse/*.py::STG_COLUMNS`, перестановка которых СОВМЕСТИМА по типу и
#: потому проходит молча (`city_name`/`employer_name`, `salary_min_rub`/`salary_max_rub`,
#: `experience`/`schedule`, `is_remote`/`remote_mentioned`, `source`/`name`). Перестановка
#: несовместимых по типу (`salary_max_rub`/`salary_currency`) роняет саму загрузку.
FACT_COLUMNS = ("id", "source", "city", "employer", "experience", "schedule",
                "is_remote", "remote_mentioned", "salary_min_rub", "salary_max_rub")

#: Ожидаемое содержимое факта — по строке на запись фикстуры, отсортировано по id.
#: Значения выведены из спецификации разбора (`docs/etl.md`, справочники
#: `domain.py::EXPERIENCE`/`SCHEDULE`, `REMOTE_LIKE_CODES`, курсы `fixtures/fx_rates.json`)
#: и записаны литералом ДО первого прогона. Курсы синтетические и круглые, поэтому круглые
#: и рубли: 4500 BYN -> 4500 / 2.5 * 100 = 180 000; 45 000 EUR -> 45 000 / 0.5 * 100.
#: Одно и то же ожидание для всех трёх движков — в этом и смысл: разойдясь, адаптеры
#: назовут виновника строкой, а не разницей в счётчике.
EXPECTED_FACT = [
    ("121000001", "hh", "Москва", "Acme", "1–3 года", "Удалённо", True, True, 250000, 350000),
    ("121000002", "hh", "Санкт-Петербург", "Beta", "3–6 лет", "Офис", False, False, 180000, None),
    # `flexible` -> «Гибрид» и is_remote=True: удалёнкоподобность = remote + flexible,
    # как у родителя (`hrwork/domain/schedule.py::REMOTE_LIKE_CODES`)
    ("121000003", "hh", "Москва", "Acme", "6+ лет", "Гибрид", True, False, None, None),
    # пустые строки внешнего источника -> None (город, работодатель, опыт, график),
    # а не пустая подпись: иначе в измерении заводится безымянный работодатель
    (ID_ARBEITNOW_A, "arbeitnow", None, None, None, None, False, False, 9000000, 12000000),
    (ID_ARBEITNOW_B, "arbeitnow", None, None, None, None, False, False, 9000000, 12000000),
    ("getmatch_54219", "getmatch", "Казань", "Gamma", "3–6 лет", "Офис", False, False,
     261000, 348000),
    # remote_mentioned=True от слова «remote» в тексте — ОТДЕЛЬНОЕ понятие от формата работы
    (ID_HIMALAYAS, "himalayas", "Remote", "Northwind", None, "Удалённо", True, True,
     12000000, 16000000),
    # `shift` — незнакомый непустой код графика: проходит как есть, без подписи
    ("hirify_733072", "hirify", "Минск", "Delta", "Без опыта", "shift", False, False,
     180000, 240000),
    ("talanto_e9f687b5-2c41-4a0e-9d3f-77b1a5c0d842", "talanto", CITY_AGGREGATOR,
     "Talanto Client", "1–3 года", "Офис", False, False, None, None),
]

# Факт читается НЕ тем адаптером, который его писал: сверять запись её же кодом значит
# проверять адаптер им самим. У Postgres и MS SQL факт нормализован в звезду (город и
# работодатель — в измерениях), у ClickHouse это широкая таблица, поэтому запроса два.
STAR_SELECT = """
SELECT v.id, v.source, c.name, e.name, v.experience, v.schedule,
       v.is_remote, v.remote_mentioned, v.salary_min_rub, v.salary_max_rub
FROM core.vacancies v
LEFT JOIN core.cities c    ON c.id = v.city_id
LEFT JOIN core.employers e ON e.id = v.employer_id
"""
WIDE_SELECT = """
SELECT id, source, city, employer, experience, schedule,
       is_remote, remote_mentioned, salary_min_rub, salary_max_rub
FROM hh.vacancies FORMAT JSONEachRow
"""


def _read_postgres(cfg: Settings) -> list[tuple]:
    import psycopg2
    conn = psycopg2.connect(**cfg.pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(STAR_SELECT)
            return cur.fetchall()
    finally:
        conn.close()


def _read_mssql(cfg: Settings) -> list[tuple]:
    import pymssql  # ленивый импорт: драйвер опционален, как и в самом адаптере
    with pymssql.connect(charset="UTF-8", **cfg.mssql_dsn) as conn, conn.cursor() as cur:
        cur.execute(STAR_SELECT)
        return cur.fetchall()


def _read_clickhouse(cfg: Settings) -> list[tuple]:
    url = cfg.ch_url if cfg.ch_url.endswith("/") else cfg.ch_url + "/"
    req = urllib.request.Request(url, data=WIDE_SELECT.encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
    return [tuple(json.loads(line)[col] for col in FACT_COLUMNS)
            for line in body.splitlines() if line.strip()]


READERS = {"postgres": _read_postgres, "clickhouse": _read_clickhouse, "mssql": _read_mssql}


def _normalized(rows) -> list[tuple]:
    """Выдача трёх драйверов -> один вид, чтобы ожидание было ОДНО на все движки.

    Различие типов — свойство движков (`docs/warehouses.md`), а не разница данных:
    NUMERIC/DECIMAL приезжают из psycopg2 и pymssql как `Decimal`, из ClickHouse — числом
    JSON; BOOLEAN/BIT/UInt8 — как bool или как 0/1. Суммы целые по построению
    (`rates.to_rub` округляет), поэтому `int()` ничего не теряет.
    """
    out = []
    for (vid, source, city, employer, experience, schedule,
         remote, mentioned, smin, smax) in rows:
        out.append((str(vid), str(source), city, employer, experience, schedule,
                    bool(remote), bool(mentioned),
                    None if smin is None else int(smin),
                    None if smax is None else int(smax)))
    # Сортировка в Python, а не ORDER BY: порядок строк в MS SQL зависит от collation
    # сервера и мог бы разойтись с Postgres на тех же ключах — сравнивать надо данные,
    # а не настройку инстанса.
    return sorted(out, key=lambda row: row[0])


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


@pytest.mark.parametrize("backend", TARGETS)   # postgres, clickhouse, mssql
def test_loaded_fact_carries_source_values(cfg, backend):
    # 09.08.2026: round-trip проверялся ТОЛЬКО числом строк, а список колонок продублирован
    # в `warehouse/postgres.py::STG_COLUMNS` и `warehouse/mssql.py::STG_COLUMNS` и держится
    # на ручном совпадении порядка с кортежом `vac_rows`. Переставь в списке `city_name`
    # и `employer_name` — получишь город в графе работодателя: 9 строк, зелёный CI и
    # совравшие витрины `mart.city_stats` / `mart.top_employers`.
    pl = build_pipeline([backend], cfg)
    pl.run(["all"])
    assert _normalized(READERS[backend](cfg)) == EXPECTED_FACT


def test_all_backends_agree(cfg):
    # 09.08.2026: было `assert all(c == EXPECTED for c in counts.values())` — утверждение
    # ВАКУУМНО истинно на пустом словаре. Выпади бэкенд из REGISTRY, «все движки сошлись»
    # осталось бы зелёным на двух движках и даже на нуле. Литеральный словарь целиком
    # фиксирует и состав движков, и число строк, и называет виновника расхождения.
    pl = build_pipeline(list(TARGETS), cfg)
    pl.run(["all"])
    counts = {wh.name: wh.count() for wh in pl.warehouses}
    assert counts == {"postgres": 9, "clickhouse": 9, "mssql": 9}
