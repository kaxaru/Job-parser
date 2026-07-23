"""PoC поиска вакансий в PostgreSQL — учебный пример «нужен ли tsvector / нужен ли ES».

Грузит свежие вакансии (data/vacancies_raw.json) в ПЛОСКУЮ таблицу search_demo.vacancies
с полным текстом (name + description), строит индексы трёх типов:
  - GIN по tsvector  -> полнотекстовый поиск с морфологией и ранжированием (BM25-подобно);
  - GIN trigram      -> поиск по опечаткам/похожести (pg_trgm);
  - btree city/sal   -> обычные фильтры.
Идемпотентно (DROP SCHEMA ... CASCADE). Запуск:
  .venv3/Scripts/python.exe dwh_demo/search_demo/load.py
"""
import json
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

# Добираемся до корня hr_work, чтобы переиспользовать парсер ленты (techs/salary/city).
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from hrwork.config import PG_DSN  # noqa: E402   # PG_DSN — единый источник
from hrwork.domain.parsing import parse_vacancy  # noqa: E402   # data/ -> domain/ (рефактор)

RAW = ROOT / "data" / "vacancies_raw.json"

_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """description_html -> чистый текст (для to_tsvector)."""
    return _TAG.sub(" ", s or "").replace("&nbsp;", " ").strip()


DDL = """
DROP SCHEMA IF EXISTS search_demo CASCADE;
CREATE SCHEMA search_demo;
CREATE TABLE search_demo.vacancies (
    -- id — text: основной проект неймспейсит id по источникам (hirify_/talanto_<uuid>),
    -- bigint + int(v.id) ронял 2/3 записей (hirify/talanto) на ValueError.
    id          text PRIMARY KEY,
    source      text,          -- портал: hh | hirify | talanto (для фильтра источника)
    name        text NOT NULL,
    employer    text,
    city        text,
    experience  text,
    schedule    text,
    is_remote   boolean,
    sal_from    integer,
    sal_to      integer,
    sal_mid     integer,
    currency    text,
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
    rows = []
    for item in raw:
        v = parse_vacancy(item)
        desc = strip_html(item.get("description_html") or "") \
            or (item.get("snippet") or {}).get("requirement", "")
        # Домен-рефактор: salary -> Salary-VO (.frm/.to/.mid), experience/schedule -> enum.
        sal = v.salary
        rows.append((
            v.id, v.source, v.name, v.employer,
            v.city, v.experience.label if v.experience else None, v.schedule.hh_code,
            v.is_remote(),
            sal.frm if sal else None, sal.to if sal else None, sal.mid if sal else None,
            sal.currency if sal else None,
            item.get("alternate_url"), v.techs, v.created_at, desc,
        ))

    conn = psycopg2.connect(**PG_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)
    execute_values(
        cur,
        "INSERT INTO search_demo.vacancies "
        "(id,source,name,employer,city,experience,schedule,is_remote,"
        " sal_from,sal_to,sal_mid,currency,url,techs,created_at,description) "
        "VALUES %s ON CONFLICT (id) DO NOTHING",
        rows, page_size=1000,
    )
    # Индексы трёх типов — потом сравним, какой когда срабатывает.
    cur.execute("CREATE INDEX ix_doc_gin   ON search_demo.vacancies USING GIN (doc);")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    cur.execute("CREATE INDEX ix_name_trgm ON search_demo.vacancies USING GIN (name gin_trgm_ops);")
    cur.execute("CREATE INDEX ix_city      ON search_demo.vacancies (city);")
    cur.execute("CREATE INDEX ix_salmid    ON search_demo.vacancies (sal_mid);")
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
