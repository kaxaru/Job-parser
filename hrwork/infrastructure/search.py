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
import contextlib
import os
import threading
from typing import Any, NamedTuple

# DSN живёт в одном месте — hrwork/config.PG_DSN (там же грузится .env). Здесь только
# имя таблицы (специфично поиску). Спайк (load.py/bench.py) тоже берёт PG_DSN из config.
from hrwork.config import PG_DSN
from hrwork.domain.freshness import FRESH_DAYS, GHOST_DAYS  # единый источник порогов (как в ленте)

TABLE = os.getenv("SEARCH_TABLE", "search_demo.vacancies")
LIMIT_MAX = 100   # потолок выдачи, чтобы не выкачать всё одним запросом

# work_mem задаётся НА СОЕДИНЕНИИ, а не глобально в контейнере: подкрутка нужна этому
# запросу, а БД делит с нами ETL dwh_demo; к тому же настройка переживает пересоздание
# контейнера (docker compose down -v), в отличие от правки postgresql.conf.
#
# Зачем: count(*) OVER() заставляет WindowAgg буферизовать ВЕСЬ набор совпадений до выдачи
# первой строки. На стартовом показе ленты поиска (запрос без q — «топ по зарплате») это
# все 87k строк по ~525 байт ≈ 45 МБ, и при дефолтных 4 МБ Postgres сваливает ~86 МБ во
# временные файлы. Замер 01.08.2026 на этом запросе: 541 мс -> 220 мс (temp-спилл = 321 мс).
WORK_MEM = os.getenv("SEARCH_WORK_MEM", "64MB")

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
            f"SELECT {_COLS}, {self.rank} AS rank, {self.snippet} AS snippet "
            f"FROM {self.source}{where_sql} "
            f"ORDER BY {self.order} LIMIT %(limit)s OFFSET %(offset)s"
        )

    def count_sql(self, where_sql: str) -> str:
        """Точный total для пагинации — ОТДЕЛЬНЫМ запросом, а не `count(*) OVER()`.

        Имя НЕ `count`: _Mode — NamedTuple, а у кортежа уже есть `count() -> int`, и метод
        с другой сигнатурой его перекрывал бы (mypy: invalid override).

        Оконная функция заставляет WindowAgg материализовать весь набор совпадений вместе
        с широкими колонками (doc/description, ~525 байт на строку) до выдачи первой строки,
        хотя наружу уходит `limit` строк. Отдельный count(*) читает те же строки, но без
        payload. Замер 01.08.2026 (work_mem=64MB): запрос без q 209 -> 150 мс, q=python
        99 -> 89 мс; сам count(*) стоит 7 мс. Оба запроса идут в ОДНОЙ транзакции,
        поэтому total согласован со страницей."""
        return f"SELECT count(*) AS total FROM {self.source}{where_sql}"


# q задан -> FTS: tsquery в FROM, ранг ts_rank, сниппет с подсветкой, сорт по релевантности.
_FTS = _Mode(
    source=f"{TABLE}, websearch_to_tsquery('russian', %(q)s) q",
    rank="ts_rank(doc, q)",
    snippet=f"'…' || {_HEADLINE} || '…'",   # выдержка из середины -> «…» по обоим краям
    # id в хвосте ORDER BY — ТАЙ-БРЕЙКЕР, а не украшение: без него порядок строк с равными
    # (rank, sal_mid) не определён, и постраничная выдача через OFFSET дублирует одни
    # вакансии и пропускает другие. Замер 01.08.2026: страницы 1 и 2 по q=python
    # пересекались по 3 id из 20.
    order="rank DESC, sal_mid DESC NULLS LAST, id",
)
# q пустой -> без tsquery, ранга нет, сниппет = обрезка описания, сорт по зарплате.
_PLAIN = _Mode(
    source=TABLE,
    rank="NULL::real",
    snippet="left(coalesce(description, ''), 180) || '…'",   # начало описания -> «…» в конце
    order="sal_mid DESC NULLS LAST, id",   # id — тай-брейкер, см. _FTS.order
)


def _mode_for(q: str | None) -> _Mode:
    """Фабрика режима выдачи: FTS при наличии q, иначе фильтр-режим."""
    return _FTS if q else _PLAIN


class SearchUnavailable(RuntimeError):
    """БД недоступна — сервер отдаёт 503, лента продолжает работать."""


def _connect() -> Any:
    # psycopg2 — лениво (он в requirements.txt, т.е. жёсткая зависимость; это НЕ про
    # graceful degradation). Причина: чистые _build_sql/_mode_for остаются импортируемыми
    # и тестируемыми БЕЗ драйвера (так инспектируем сборку SQL в dbg_search.py без БД).
    # Драйвер нужен лишь на сам коннект; падение БД -> SearchUnavailable (503).
    import psycopg2
    try:
        return psycopg2.connect(**PG_DSN, options=f"-c work_mem={WORK_MEM}")
    except Exception as e:
        raise SearchUnavailable(f"PostgreSQL недоступен ({PG_DSN['host']}:{PG_DSN['port']}): {e}") from e


# ── Пул соединений ──────────────────────────────────────────────────────────────────────
# Коннект на КАЖДЫЙ запрос стоил 18.4 мс при 9.8 мс на сами запросы (замер 01.08.2026),
# то есть 65 % времени уходило на установку соединения, а не на работу.
#
# Именно ПУЛ, а не одно общее соединение: сервер — ThreadingHTTPServer, каждый запрос
# в своём потоке, а курсоры psycopg2 между потоками делить нельзя.
_POOL: Any = None
_POOL_LOCK = threading.Lock()
POOL_MAX = int(os.getenv("SEARCH_POOL_MAX", "8"))


def _pool() -> Any:
    """Ленивый ThreadedConnectionPool (двойная проверка под локом — на гонке потоков
    при первом запросе создаётся ровно один пул)."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                from psycopg2 import pool
                try:
                    _POOL = pool.ThreadedConnectionPool(
                        1, POOL_MAX, **PG_DSN, options=f"-c work_mem={WORK_MEM}")
                except Exception as e:
                    raise SearchUnavailable(
                        f"PostgreSQL недоступен ({PG_DSN['host']}:{PG_DSN['port']}): {e}") from e
    return _POOL


def _drop_pool() -> None:
    """Закрыть пул целиком: после рестарта БД в нём лежат мёртвые соединения, и брать
    оттуда бессмысленно — следующий вызов создаст пул заново."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            with contextlib.suppress(Exception):
                _POOL.closeall()
            _POOL = None


def _where(q: str | None, city: str | None, sal_min: int,
           fresh: str | None = None,
           source: str | None = None) -> tuple[str, dict[str, Any]]:
    """(where_sql, params) — общая часть страницы и счётчика: оба обязаны фильтровать
    ОДИНАКОВО, иначе total разъедется с выдачей. Новый фильтр = строка в кортеже."""
    where: list[str] = []
    params: dict[str, Any] = {}
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
    return where_sql, params


def _build_sql(q: str | None, city: str | None, sal_min: int,
               fresh: str | None = None,
               source: str | None = None) -> tuple[str, dict[str, Any]]:
    """(sql страницы, params) из входов. Режим выбирает фабрика _mode_for, сборку SELECT
    делает сам режим (mode.select). Счётчик — _build_count_sql на том же WHERE."""
    where_sql, params = _where(q, city, sal_min, fresh, source)
    return _mode_for(q).select(where_sql), params


def _build_count_sql(q: str | None, city: str | None, sal_min: int,
                     fresh: str | None = None,
                     source: str | None = None) -> tuple[str, dict[str, Any]]:
    """(sql счётчика, params) — тот же WHERE, что у страницы, но без payload и сортировки."""
    where_sql, params = _where(q, city, sal_min, fresh, source)
    return _mode_for(q).count_sql(where_sql), params


def _run(conn: Any, page_sql: str, count_sql: str,
         params: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Страница + точный total в ОДНОЙ транзакции (`with conn`): иначе между двумя
    запросами мог бы влезть переналив таблицы, и «N–M из total» показал бы
    несогласованные числа."""
    with conn, conn.cursor() as cur:
        cur.execute(page_sql, params)
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute(count_sql, params)     # лишние ключи params (limit/offset) psycopg2 игнорирует
        total = int(cur.fetchone()[0])
        return rows, total


def _fetch(page_sql: str, count_sql: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Выполнить пару запросов на соединении ИЗ ПУЛА. Изоляция I/O — весь psycopg2 здесь.

    Ретрай ровно один и только на обрыве соединения (OperationalError/InterfaceError):
    после рестарта hh-postgres в пуле лежат мёртвые соединения, и без ретрая первый
    запрос отдавал бы 500 на живой БД. Ошибки САМОГО запроса (синтаксис, нет таблицы)
    к этим типам не относятся и наверх уходят сразу, без повтора."""
    import psycopg2
    from psycopg2.pool import PoolError
    stale = (psycopg2.OperationalError, psycopg2.InterfaceError)
    for attempt in (1, 2):
        pool, conn, pooled = _pool(), None, True
        try:
            try:
                conn = pool.getconn()
            except PoolError:
                # Пул исчерпан. getconn НЕ ждёт освобождения, а бросает сразу, поэтому
                # всплеск параллельных запросов сверх POOL_MAX ронял бы поиск в 500
                # (воспроизведено 01.08.2026: 12 потоков на пул из 8). Деградируем до
                # отдельного соединения: +18 мс на такой запрос, но он проходит.
                conn, pooled = _connect(), False
            return _run(conn, page_sql, count_sql, params)
        except stale:
            if conn is not None:
                with contextlib.suppress(Exception):
                    pool.putconn(conn, close=True) if pooled else conn.close()
                conn = None
            _drop_pool()
            if attempt == 2:
                raise SearchUnavailable(
                    f"PostgreSQL недоступен ({PG_DSN['host']}:{PG_DSN['port']})") from None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    pool.putconn(conn) if pooled else conn.close()
    raise AssertionError("недостижимо: цикл ретрая всегда возвращает или бросает")


def search(q: str | None = None, city: str | None = None, sal_min: int = 0,
           limit: int = 20, offset: int = 0, fresh: str | None = None,
           source: str | None = None) -> dict[str, Any]:
    """Полнотекстовый поиск с ранжированием и подсветкой. Возвращает dict с total
    (для пагинации) и results. `fresh` (fresh|recent|ghost) фильтрует по классу свежести,
    `source` (hh|hirify|talanto) — по порталу. Детали SQL — в _build_sql()."""
    q = q.strip() if (q and q.strip()) else None
    sal_min = int(sal_min or 0)
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    offset = max(0, int(offset or 0))
    fresh = fresh if fresh in _FRESH_WHERE else None
    source = source if source in SOURCES else None

    where_sql, params = _where(q, city, sal_min, fresh, source)
    mode = _mode_for(q)
    params.update(limit=limit, offset=offset)
    rows, total = _fetch(mode.select(where_sql), mode.count_sql(where_sql), params)

    for r in rows:
        if r.get("age_days") is not None:
            r["age_days"] = int(r["age_days"])
        if r["rank"] is not None:
            r["rank"] = round(float(r["rank"]), 4)
    return {
        "query": q, "city": city, "sal_min": sal_min, "fresh": fresh, "source": source,
        "total": total, "count": len(rows), "limit": limit, "offset": offset,
        "results": rows,
    }
