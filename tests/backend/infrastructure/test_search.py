"""Сборка SQL поиска (без БД): фильтры, режимы, класс свежести. _build_sql — чистая."""
import pytest

from hrwork.domain.freshness import FRESH_DAYS, GHOST_DAYS
from hrwork.infrastructure import search as S


def test_cols_expose_age_fresh_and_source():
    # карточка отдаёт возраст, класс свежести и источник (для бейджей и фильтров в /search)
    assert "AS age_days" in S._COLS and "AS fresh" in S._COLS
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
    assert "websearch_to_tsquery" in sql and params["q"] == "python"
    assert "doc @@ q" in sql


@pytest.mark.parametrize("fresh,marker", [
    ("fresh",  f"<= {FRESH_DAYS}"),
    ("recent", f"> {FRESH_DAYS}"),
    ("ghost",  f"> {GHOST_DAYS}"),
])
def test_fresh_filter_adds_band_clause(fresh, marker):
    sql, _ = S._build_sql(q=None, city=None, sal_min=0, fresh=fresh)
    assert "created_at IS NOT NULL" in sql and marker in sql


def test_unknown_fresh_value_ignored():
    # значение вне белого списка не добавляет WHERE (и не может протащить SQL-инъекцию)
    sql, _ = S._build_sql(q=None, city=None, sal_min=0, fresh="'; DROP TABLE x;--")
    assert "DROP TABLE" not in sql
    assert "created_at IS NOT NULL" not in sql


def test_filters_combine_with_and():
    sql, params = S._build_sql(q="go", city="Москва", sal_min=100000, fresh="fresh")
    assert sql.count(" AND ") >= 3                  # q + city + sal + fresh
    assert params["city"] == "Москва" and params["sal"] == 100000
