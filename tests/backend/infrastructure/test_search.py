"""Сборка SQL поиска (без БД): фильтры, режимы, класс свежести. _build_sql — чистая."""
import datetime

import pytest

from hrwork.domain import freshness
from hrwork.infrastructure import search as S

# Возраст в SQL = floor полных суток от now(), ровно как `freshness.py::_days_between`.
# Литерал спецификации: /api/search и лента обязаны звать один и тот же класс свежести
# одной и той же вакансии.
AGE_SQL = "floor(extract(epoch from now() - created_at) / 86400)"


def test_cols_expose_age_fresh_and_source():
    # карточка отдаёт возраст, класс свежести и источник (для бейджей и фильтров в /search)
    assert "AS age_days" in S._COLS
    assert "AS fresh" in S._COLS
    assert "source" in S._COLS


@pytest.mark.parametrize("src,expected", [
    ("hh", True), ("hirify", True), ("talanto", True),
    ("evil", False), (None, False), ("'; DROP TABLE x;--", False),
])
def test_source_filter_whitelist(src, expected):
    sql, params = S._build_sql(q=None, city=None, sal_min=0, source=src)
    assert ("source = %(source)s" in sql) is expected
    assert ("source" in params) is expected        # мусор/None не протаскивается в WHERE


def test_plain_mode_without_query():
    sql, params = S._build_sql(q=None, city=None, sal_min=0)
    assert "websearch_to_tsquery" not in sql       # без q — не FTS
    assert "q" not in params


def test_fts_mode_with_query():
    sql, params = S._build_sql(q="python", city=None, sal_min=0)
    assert "websearch_to_tsquery" in sql
    assert params["q"] == "python"
    assert "doc @@ q" in sql


# ── Сниппет: HTML экранируется в SQL, а обрезка идёт ДО экранирования ───────────────────
# Сниппет — единственное поле выдачи, которое страница поиска вставляет в DOM без `esc()`
# (ради подсветки `<mark>`), поэтому экранирование обязано случиться в SQL. Обрезка ПОСЛЕ
# экранирования резала по экранированному тексту и рвала сущность пополам («&amp;» -> «&am»),
# а обрывок уходил в разметку как есть (аудит 09.08.2026). Литералы спецификации: три
# replace в порядке `&` `<` `>` поверх УЖЕ обрезанных 180 символов исходного описания.
_ESC = "'&', '&amp;'), '<', '&lt;'), '>', '&gt;')"


def test_plain_snippet_escapes_the_cut_text_instead_of_cutting_the_escaped_one():
    sql, _ = S._build_sql(q=None, city=None, sal_min=0)
    assert f"replace(replace(replace(left(coalesce(description, ''), 180), {_ESC} || '…' AS snippet" in sql


def test_fts_snippet_escapes_whole_description_before_highlighting():
    """Экранируем ДО ts_headline, чтобы его `<mark>` остались единственной разметкой."""
    sql, _ = S._build_sql(q="python", city=None, sal_min=0)
    assert f"ts_headline('russian', replace(replace(replace(coalesce(description, ''), {_ESC}, q, " in sql


# Пороги — ЛИТЕРАЛЫ спецификации (30/60), а не FRESH_DAYS/GHOST_DAYS из того же места,
# что и реализация: сломанная константа ломала бы обе стороны одинаково (аудит 09.08.2026).
@pytest.mark.parametrize("fresh,marker", [
    ("fresh",  "<= 30"),
    ("recent", "> 30"),
    ("ghost",  "> 60"),
])
def test_fresh_filter_adds_band_clause(fresh, marker):
    sql, _ = S._build_sql(q=None, city=None, sal_min=0, fresh=fresh)
    assert "created_at IS NOT NULL" in sql
    assert marker in sql


# ── Регрессия 08.08.2026 (аудит, п.32): возраст в SQL расходился с доменом ──────────────
# SQL считал КАЛЕНДАРНУЮ разницу дат в таймзоне сессии Postgres (now()::date -
# created_at::date), домен — полные сутки от UTC-now ((later - earlier).days). Вакансия,
# созданная 2026-07-08T20:00Z, в момент 2026-08-08T06:00Z была FRESH в ленте (30 дн) и
# RECENT в /api/search (31 дн) — фильтр fresh=fresh её терял. То же на границе 60/61.

@pytest.mark.parametrize("created,now_iso,expected_age", [
    ("2026-07-08T20:00:00+00:00", "2026-08-08T06:00:00+00:00", 30),   # 30 сут 10 ч -> FRESH
    ("2026-06-08T20:00:00+00:00", "2026-08-08T06:00:00+00:00", 60),   # 60 сут 10 ч -> RECENT
])
def test_domain_age_is_floor_of_full_utc_days(created, now_iso, expected_age):
    """Спецификация, которой обязан следовать SQL: возраст — полные сутки, не разница дат."""
    assert freshness.age_days(created, datetime.datetime.fromisoformat(now_iso)) == expected_age


def test_age_column_counts_full_days_like_domain():
    assert S._AGE == AGE_SQL


def test_age_does_not_depend_on_session_timezone():
    # приведение к ::date считает возраст в таймзоне сессии Postgres, а домен — в UTC
    sql, _ = S._build_sql(q=None, city=None, sal_min=0, fresh="fresh")
    assert "::date" not in sql


def test_fresh_case_labels_rows_with_domain_codes_and_thresholds():
    expected = ("CASE WHEN created_at IS NULL THEN 'unknown' "
                f"WHEN {AGE_SQL} <= 30 THEN 'fresh' "
                f"WHEN {AGE_SQL} <= 60 THEN 'recent' "
                "ELSE 'ghost' END")
    assert expected == S._FRESH_CASE


@pytest.mark.parametrize("fresh,expected_clause", [
    ("fresh",  f"created_at IS NOT NULL AND {AGE_SQL} <= 30"),
    ("recent", f"created_at IS NOT NULL AND {AGE_SQL} > 30 AND {AGE_SQL} <= 60"),
    ("ghost",  f"created_at IS NOT NULL AND {AGE_SQL} > 60"),
])
def test_fresh_filter_selects_the_same_band_it_labels(fresh, expected_clause):
    sql, _ = S._build_sql(q=None, city=None, sal_min=0, fresh=fresh)
    assert expected_clause in sql


def test_unknown_fresh_value_ignored():
    # значение вне белого списка не добавляет WHERE (и не может протащить SQL-инъекцию)
    sql, _ = S._build_sql(q=None, city=None, sal_min=0, fresh="'; DROP TABLE x;--")
    assert "DROP TABLE" not in sql
    assert "created_at IS NOT NULL" not in sql


def test_filters_combine_with_and():
    sql, params = S._build_sql(q="go", city="Москва", sal_min=100000, fresh="fresh")
    # ТОЧНОЕ число, а не `>= 3`: четыре предиката (q + city + sal + fresh) склеиваются
    # тремя « AND », и четвёртое живёт внутри самой клаузы свежести
    # («created_at IS NOT NULL AND <возраст> <= 30»).
    assert sql.count(" AND ") == 4
    assert params["city"] == "Москва"
    assert params["sal"] == 100000


# ── total считается отдельным запросом, а не оконной функцией (01.08.2026) ──────────────
# count(*) OVER() заставлял WindowAgg материализовать весь набор совпадений с широкими
# колонками: стартовый показ /search (без q) — 209 мс против 150 мс с отдельным count(*).

def test_page_query_has_no_window_count():
    sql, _ = S._build_sql(q=None, city=None, sal_min=0)
    assert "count(*) OVER()" not in sql


@pytest.mark.parametrize("q", [None, "python"])
def test_count_query_selects_total_without_payload(q):
    sql, _ = S._build_count_sql(q=q, city=None, sal_min=0)
    assert sql.startswith("SELECT count(*) AS total FROM ")
    assert "LIMIT" not in sql                              # счётчику не нужна страница
    assert "ORDER BY" not in sql                           # ... и сортировка
    assert "ts_headline" not in sql                        # и тем более сниппет


@pytest.mark.parametrize("q", [None, "python"])
def test_count_and_page_filter_identically(q):
    """Разъехавшийся WHERE дал бы «N–M из total» с чужим total."""
    page, page_params = S._build_sql(q=q, city="Москва", sal_min=50000, fresh="fresh")
    count, count_params = S._build_count_sql(q=q, city="Москва", sal_min=50000, fresh="fresh")
    where_of = lambda s: s.split(" WHERE ", 1)[1].split(" ORDER BY ")[0]   # noqa: E731
    assert where_of(page) == where_of(count)
    assert page_params == count_params


# ── Регрессия 01.08.2026: страницы 1 и 2 по q=python пересекались по 3 id из 20 ─────────
# ORDER BY без уникального тай-брейкера не задаёт порядок строк с равными ключами,
# поэтому OFFSET-пагинация дублировала одни вакансии и пропускала другие.

@pytest.mark.parametrize("q,expected_tail", [
    (None,     "sal_mid DESC NULLS LAST, id"),
    ("python", "rank DESC, sal_mid DESC NULLS LAST, id"),
])
def test_order_by_ends_with_id_tiebreaker(q, expected_tail):
    sql, _ = S._build_sql(q=q, city=None, sal_min=0)
    order_by = sql.split(" ORDER BY ", 1)[1].split(" LIMIT ")[0]
    assert order_by == expected_tail


# ── Пул соединений: поведение на отказах (01.08.2026) ───────────────────────────────────

class _FakePool:
    """Пул-заглушка: getconn отдаёт заранее заданные соединения либо бросает."""

    def __init__(self, conns=(), raises=None):
        self.conns = list(conns)
        self.raises = raises
        self.returned: list[tuple[object, bool]] = []   # (conn, closed)

    def getconn(self):
        if self.raises is not None:
            raise self.raises
        return self.conns.pop(0)

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))


def test_exhausted_pool_degrades_to_direct_connection(monkeypatch):
    """getconn psycopg2 при исчерпании НЕ ждёт, а бросает PoolError. Всплеск параллельных
    запросов не должен ронять поиск: берём отдельное соединение и закрываем его."""
    from psycopg2.pool import PoolError

    class _Conn:
        closed = False

        def close(self):
            self.closed = True

    direct = _Conn()
    used: list[object] = []
    monkeypatch.setattr(S, "_pool", lambda: _FakePool(raises=PoolError("exhausted")))
    monkeypatch.setattr(S, "_connect", lambda: direct)
    monkeypatch.setattr(S, "_run", lambda conn, p, c, prm: (used.append(conn), ([{"id": "1"}], 7))[1])

    assert S._fetch("PAGE", "COUNT", {}) == ([{"id": "1"}], 7)
    assert used == [direct]           # запрос ушёл на отдельное соединение, а не упал
    assert direct.closed is True      # и оно закрыто, а не утекло (в пул его класть нельзя)


def test_dead_connection_is_retried_once_then_reported_unavailable(monkeypatch):
    """После рестарта hh-postgres в пуле лежат мёртвые соединения: первый заход обязан
    выбросить пул и повторить, а не отдать 500 на живой БД."""
    import psycopg2
    calls: list[str] = []
    monkeypatch.setattr(S, "_pool", lambda: _FakePool(conns=[object(), object()]))
    monkeypatch.setattr(S, "_drop_pool", lambda: calls.append("dropped"))

    def _run_ok_on_second(conn, page, count, params):
        calls.append("run")
        if calls.count("run") == 1:
            raise psycopg2.OperationalError("server closed the connection")
        return ([{"id": "2"}], 1)

    monkeypatch.setattr(S, "_run", _run_ok_on_second)
    assert S._fetch("PAGE", "COUNT", {}) == ([{"id": "2"}], 1)
    assert calls == ["run", "dropped", "run"]


def test_dead_connection_twice_raises_search_unavailable(monkeypatch):
    """БД действительно лежит -> SearchUnavailable, сервер отдаст 503, лента живёт."""
    import psycopg2
    monkeypatch.setattr(S, "_pool", lambda: _FakePool(conns=[object(), object()]))
    monkeypatch.setattr(S, "_drop_pool", lambda: None)

    def _always_dead(conn, page, count, params):
        raise psycopg2.OperationalError("could not connect")

    monkeypatch.setattr(S, "_run", _always_dead)
    with pytest.raises(S.SearchUnavailable):
        S._fetch("PAGE", "COUNT", {})
