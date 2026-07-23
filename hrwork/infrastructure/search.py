"""Поиск вакансий через PostgreSQL full-text (tsvector) — прод-путь для /api/search.

Зачем отдельный модуль (а не в server.py): SoC — здесь только логика поиска,
сервер лишь маршрутизирует. psycopg2 импортится ЛЕНИВО внутри _connect(), поэтому
без БД остальной `hh.py serve` (лента + отметки) продолжает работать.

Структура: _build_sql() — сборка SQL (без БД) · _fetch() — выполнение (I/O) ·
search() — нормализация входов + оркестрация + форма ответа.

Безопасность: все пользовательские входы (q/city/sal/limit/offset) идут ТОЛЬКО как
параметры запроса (psycopg2 экранирует) — SQL-инъекция невозможна. Имя таблицы берётся
из окружения (не из запроса), поэтому его подстановка в текст SQL безопасна.
"""
import os
from typing import NamedTuple

# DSN живёт в одном месте — hrwork/config.PG_DSN (там же грузится .env). Здесь только
# имя таблицы (специфично поиску). Спайк (load.py/bench.py) тоже берёт PG_DSN из config.
from hrwork.config import PG_DSN
from hrwork.domain.freshness import FRESH_DAYS, GHOST_DAYS  # единый источник порогов (как в ленте)

TABLE = os.getenv("SEARCH_TABLE", "search_demo.vacancies")
LIMIT_MAX = 100   # потолок выдачи, чтобы не выкачать всё одним запросом

# Возраст и класс свежести считаются В ЗАПРОСЕ (live от now()), а не при загрузке — как в ленте,
# где age_days/fresh_class тоже производные. Пороги — из домена (FRESH_DAYS/GHOST_DAYS).
_AGE = "(now()::date - created_at::date)"
_FRESH_CASE = (
    f"CASE WHEN created_at IS NULL THEN 'unknown' "
    f"WHEN {_AGE} <= {FRESH_DAYS} THEN 'fresh' "
    f"WHEN {_AGE} <= {GHOST_DAYS} THEN 'recent' ELSE 'ghost' END"
)
# Карточные колонки (общие для обоих режимов) + производные свежести.
_COLS = ("id, source, name, employer, city, sal_from, sal_to, sal_mid, currency, url, techs, "
         f"{_AGE} AS age_days, {_FRESH_CASE} AS fresh")

SOURCES = ("hh", "hirify", "talanto")   # белый список порталов для фильтра источника

# WHERE-фрагмент фильтра свежести по классу (null-даты в «свежие/недавние» не попадают).
_FRESH_WHERE = {
    "fresh":  f"created_at IS NOT NULL AND {_AGE} <= {FRESH_DAYS}",
    "recent": f"created_at IS NOT NULL AND {_AGE} > {FRESH_DAYS} AND {_AGE} <= {GHOST_DAYS}",
    "ghost":  f"created_at IS NOT NULL AND {_AGE} > {GHOST_DAYS}",
}
# Сниппет с подсветкой совпадений: 2 фрагмента, <mark>…</mark> (только в FTS-режиме).
_HEADLINE = (
    "ts_headline('russian', coalesce(description, ''), q, "
    "'StartSel=<mark>,StopSel=</mark>,MaxFragments=2,MaxWords=25,MinWords=12,ShortWord=2')"
)


class _Mode(NamedTuple):
    """Режим выдачи: SQL-фрагменты + умеет собрать из них SELECT (tell-don't-ask).
    Выбирается по наличию q (см. _FTS/_PLAIN)."""
    source:  str   # FROM-часть
    rank:    str   # выражение ранга
    snippet: str   # выражение сниппета
    order:   str   # ORDER BY

    def select(self, where_sql: str) -> str:
        return (
            f"SELECT {_COLS}, {self.rank} AS rank, {self.snippet} AS snippet, "
            f"count(*) OVER() AS total "
            f"FROM {self.source}{where_sql} "
            f"ORDER BY {self.order} LIMIT %(limit)s OFFSET %(offset)s"
        )


# q задан -> FTS: tsquery в FROM, ранг ts_rank, сниппет с подсветкой, сорт по релевантности.
_FTS = _Mode(
    source=f"{TABLE}, websearch_to_tsquery('russian', %(q)s) q",
    rank="ts_rank(doc, q)",
    snippet=f"'…' || {_HEADLINE} || '…'",   # выдержка из середины -> «…» по обоим краям
    order="rank DESC, sal_mid DESC NULLS LAST",
)
# q пустой -> без tsquery, ранга нет, сниппет = обрезка описания, сорт по зарплате.
_PLAIN = _Mode(
    source=TABLE,
    rank="NULL::real",
    snippet="left(coalesce(description, ''), 180) || '…'",   # начало описания -> «…» в конце
    order="sal_mid DESC NULLS LAST",
)


def _mode_for(q) -> _Mode:
    """Фабрика режима выдачи: FTS при наличии q, иначе фильтр-режим."""
    return _FTS if q else _PLAIN


class SearchUnavailable(RuntimeError):
    """БД недоступна — сервер отдаёт 503, лента продолжает работать."""


def _connect():
    # psycopg2 — лениво (он в requirements.txt, т.е. жёсткая зависимость; это НЕ про
    # graceful degradation). Причина: чистые _build_sql/_mode_for остаются импортируемыми
    # и тестируемыми БЕЗ драйвера (так инспектируем сборку SQL в dbg_search.py без БД).
    # Драйвер нужен лишь на сам коннект; падение БД -> SearchUnavailable (503).
    import psycopg2
    try:
        return psycopg2.connect(**PG_DSN)
    except Exception as e:
        raise SearchUnavailable(f"PostgreSQL недоступен ({PG_DSN['host']}:{PG_DSN['port']}): {e}") from e


def _build_sql(q, city, sal_min, fresh=None, source=None):
    """(sql, params) из входов. Фильтры — таблицей; режим выбирает фабрика _mode_for,
       а сборку SELECT делает сам режим (mode.select). Новый фильтр = строка в кортеже."""
    where, params = [], {}
    for value, key, clause in (
        (q,       "q",    "doc @@ q"),               # FTS-предикат (q также параметр FROM)
        (city,    "city", "city = %(city)s"),
        (sal_min, "sal",  "sal_mid >= %(sal)s"),
        (source if source in SOURCES else None, "source", "source = %(source)s"),
    ):
        if value:
            params[key] = value
            where.append(clause)
    if fresh in _FRESH_WHERE:                         # класс свежести — не параметр (из белого списка)
        where.append(_FRESH_WHERE[fresh])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    return _mode_for(q).select(where_sql), params


def _fetch(sql, params):
    """Выполнить запрос и вернуть строки как список dict (изоляция I/O)."""
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def search(q=None, city=None, sal_min=0, limit=20, offset=0, fresh=None, source=None) -> dict:
    """Полнотекстовый поиск с ранжированием и подсветкой. Возвращает dict с total
    (для пагинации) и results. `fresh` (fresh|recent|ghost) фильтрует по классу свежести,
    `source` (hh|hirify|talanto) — по порталу. Детали SQL — в _build_sql()."""
    q = q.strip() if (q and q.strip()) else None
    sal_min = int(sal_min or 0)
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    offset = max(0, int(offset or 0))
    fresh = fresh if fresh in _FRESH_WHERE else None
    source = source if source in SOURCES else None

    sql, params = _build_sql(q, city, sal_min, fresh, source)
    params.update(limit=limit, offset=offset)
    rows = _fetch(sql, params)

    total = rows[0]["total"] if rows else 0                # count(*) OVER() — одинаков во всех строках
    for r in rows:
        r.pop("total", None)
        if r.get("age_days") is not None:
            r["age_days"] = int(r["age_days"])
        if r["rank"] is not None:
            r["rank"] = round(float(r["rank"]), 4)
    return {
        "query": q, "city": city, "sal_min": sal_min, "fresh": fresh, "source": source,
        "total": total, "count": len(rows), "limit": limit, "offset": offset,
        "results": rows,
    }
