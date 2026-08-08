"""PoC поиска вакансий в PostgreSQL — учебный пример «нужен ли tsvector / нужен ли ES».

Грузит свежие вакансии (data/vacancies_raw.json) в ПЛОСКУЮ таблицу search_demo.vacancies
с полным текстом (name + description), строит индексы трёх типов:
  - GIN по tsvector  -> полнотекстовый поиск с морфологией и ранжированием (BM25-подобно);
  - GIN trigram      -> поиск по опечаткам/похожести (pg_trgm);
  - btree city/sal   -> обычные фильтры.
Зарплата кладётся ДВАЖДЫ: нативной вилкой (показать в карточке) и рублёвой серединкой
`sal_mid_rub` (сравнивать/сортировать). Валюты нормализованы кодом родителя.
Идемпотентно (DROP SCHEMA ... CASCADE). Запуск:
  .venv3/Scripts/python.exe dwh_demo/search_demo/load.py
"""
import json
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

# Добираемся до корня hr_work, чтобы переиспользовать парсер ленты (techs/salary/city)
# и его же курсы валют, и до dwh_demo — за общим transform стенда (strip_html).
DWH = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DWH))
from hrwork.config import PG_DSN  # noqa: E402   # PG_DSN — единый источник
from hrwork.domain.freshness import parse_dt  # noqa: E402   # наивная дата = UTC (как в домене)
from hrwork.domain.parsing import parse_vacancy  # noqa: E402   # data/ -> domain/ (рефактор)

# to_rub/resolve_currency — единственный ответ проекта на «сколько это в рублях» и «какой
# это код валюты»: ПУСТАЯ валюта даёт None (а не рубли), RUR/BYR/USDT сводятся к RUB/BYN/USD.
from hrwork.infrastructure.net.rates import (  # noqa: E402
    get_rates,
    resolve_currency,
    to_rub,
)

# strip_html — ОДИН на стенд: копия в этом файле не делала html.unescape, из-за чего
# в description доезжали `&quot;`/`&amp;`, выдача экранировала `&` второй раз («R&amp;D»
# в сниппете), а to_tsvector индексировал мусорные токены `quot`/`amp`.
from etl.domain import strip_html  # noqa: E402

RAW = ROOT / "data" / "vacancies_raw.json"


DDL = """
DROP SCHEMA IF EXISTS search_demo CASCADE;
CREATE SCHEMA search_demo;
CREATE TABLE search_demo.vacancies (
    -- id — text: основной проект неймспейсит id по источникам (hirify_/talanto_<uuid>),
    -- bigint + int(v.id) ронял 2/3 записей (hirify/talanto) на ValueError.
    id          text PRIMARY KEY,
    source      text,          -- портал: перечень задаёт родитель (hrwork/config.py::SOURCES)
    name        text NOT NULL,
    employer    text,
    city        text,
    experience  text,
    schedule    text,
    -- удалёнкоподобность = REMOTE + HYBRID (`Schedule.is_remote_like` — единственный ответ
    -- родителя на вопрос «это удалёнка?»). Имя НЕ `is_remote`: в той же базе `hh` живёт
    -- core.vacancies.is_remote, и одинаковое имя с другим смыслом уже разводило цифры.
    is_remote_like boolean,
    -- Вилка КАК ПРИШЛА (валюта портала) — только для показа в карточке.
    sal_from    integer,
    sal_to      integer,
    sal_mid     integer,
    currency    text,          -- канонический код (RUR->RUB, BYR->BYN, USDT->USD)
    -- Серединка вилки В РУБЛЯХ — ЕДИНСТВЕННАЯ колонка, по которой можно фильтровать и
    -- сортировать: sal_mid складывает 45 валют в одну шкалу, и максимум по нему давал
    -- 50 000 000 UZS на первой странице, а фильтр «з/п от 200 000» выбрасывал все
    -- долларовые вилки. Родитель тот же дефект чинил в ползунке ленты 08.08.2026
    -- (`feed.py::_salary_slider_max`). Нет курса или нет валюты -> NULL, а не ноль:
    -- вакансия выпадает из сравнения, но остаётся в выдаче.
    sal_mid_rub integer,
    url         text,
    techs       text[],
    created_at  timestamptz,   -- дата создания -> возраст/свежесть считаются в запросе (live, как в ленте)
    description text,
    -- материализованный tsvector: заголовок весом A (важнее), описание весом B.
    -- STORED -> считается при вставке, индексируется; русский конфиг = стемминг + стоп-слова.
    doc tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('russian', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('russian', coalesce(description, '')), 'B')
    ) STORED
);
"""


def main():
    with open(RAW, encoding="utf-8") as f:
        raw = json.load(f)
    # Курсы берём ОДИН РАЗ на прогон: перечитывание посреди загрузки дало бы разным
    # вакансиям разные курсы, и сортировка по sal_mid_rub перестала бы быть одной шкалой.
    rates = get_rates()
    rows = []
    for item in raw:
        v = parse_vacancy(item)
        desc = strip_html(item.get("description_html") or "") \
            or (item.get("snippet") or {}).get("requirement", "")
        # Домен-рефактор: salary -> Salary-VO (.frm/.to/.mid), experience/schedule -> enum.
        sal = v.salary
        # Канонизируем код валюты кодом родителя: в кеше рубль приезжает и как RUR (hh),
        # и как RUB (getmatch, часть talanto), плюс встречаются `eur` и `USDT` —
        # без канонизации рубль двоится в карточке и в любом фасете.
        currency = (resolve_currency(sal.currency) or None) if sal else None
        rows.append((
            v.id, v.source, v.name, v.employer,
            v.city, v.experience.label if v.experience else None, v.schedule.hh_code,
            v.is_remote_like(),
            sal.frm if sal else None, sal.to if sal else None, sal.mid if sal else None,
            currency,
            to_rub(sal.mid, currency, rates) if sal else None,
            item.get("alternate_url"), v.techs, parse_dt(v.created_at), desc,
        ))

    conn = psycopg2.connect(**PG_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)
    execute_values(
        cur,
        "INSERT INTO search_demo.vacancies "
        "(id,source,name,employer,city,experience,schedule,is_remote_like,"
        " sal_from,sal_to,sal_mid,currency,sal_mid_rub,url,techs,created_at,description) "
        "VALUES %s ON CONFLICT (id) DO NOTHING",
        rows, page_size=1000,
    )
    # Индексы трёх типов — потом сравним, какой когда срабатывает.
    cur.execute("CREATE INDEX ix_doc_gin   ON search_demo.vacancies USING GIN (doc);")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    cur.execute("CREATE INDEX ix_name_trgm ON search_demo.vacancies USING GIN (name gin_trgm_ops);")
    cur.execute("CREATE INDEX ix_city      ON search_demo.vacancies (city);")
    cur.execute("CREATE INDEX ix_salmid    ON search_demo.vacancies (sal_mid);")
    # Порядок колонок ТОЧНО повторяет ORDER BY стартового показа /search
    # (search.py::_PLAIN.order = "sal_mid DESC NULLS LAST, id"). ix_salmid для него
    # непригоден: у ASC-индекса NULL'ы в конце, и обратный проход даёт DESC NULLS FIRST —
    # планировщик такой индекс не возьмёт и уходит в Seq Scan по всем 87k строк.
    # Замер 01.08.2026: запрос без q 138 мс -> 1.3 мс (22 буфера вместо ~50 000).
    cur.execute("CREATE INDEX ix_salmid_page ON search_demo.vacancies "
                "(sal_mid DESC NULLS LAST, id);")
    # Тот же постраничный индекс по РУБЛЁВОЙ серединке — под ORDER BY, который должен
    # прийти на смену сортировке по sal_mid (см. комментарий к колонке sal_mid_rub).
    # КОМПРОМИСС: два индекса вместо одного держатся ровно до правки search.py::_PLAIN.order
    # и _FTS.order; убирать ix_salmid_page ЗАРАНЕЕ нельзя — сегодняшний прод по нему ходит,
    # и его пропажа вернула бы Seq Scan по всем строкам (замер 01.08.2026: 138 мс vs 1.3 мс).
    cur.execute("CREATE INDEX ix_salmidrub_page ON search_demo.vacancies "
                "(sal_mid_rub DESC NULLS LAST, id);")
    # ix_created окупается только на САРГАБЕЛЬНОМ предикате свежести
    # (`created_at > now() - interval '31 days'`). Сегодняшний search.py::_FRESH_WHERE
    # оборачивает колонку в floor(extract(...)), и планировщик индекс не подставляет.
    cur.execute("CREATE INDEX ix_created   ON search_demo.vacancies (created_at);")
    cur.execute("CREATE INDEX ix_source    ON search_demo.vacancies (source);")
    cur.execute("ANALYZE search_demo.vacancies;")

    cur.execute(
        "SELECT count(*), pg_size_pretty(pg_total_relation_size('search_demo.vacancies')) "
        "FROM search_demo.vacancies;"
    )
    n, size = cur.fetchone()
    print(f"OK: загружено {n} вакансий | таблица+индексы: {size}")
    conn.close()


if __name__ == "__main__":
    main()
