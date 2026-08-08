"""Юнит-тесты СХЕМ трёх хранилищ (etl/sql/**): читают DDL как текст, без БД.

Зачем текстом. Схемы применяются к живым PostgreSQL/ClickHouse/MS SQL, и настоящая
проверка — `pytest -m integration`. Но три класса дефектов видны прямо в DDL и стоят
дорого: узкий ключ роняет загрузку целиком, зарплатный агрегат не в той единице даёт
бессмысленное число на дашборде, а разъехавшиеся имена колонок ломают обещание
«одна витрина = одно число во всех движках». Эти инварианты и стережём здесь.

Спецификация метрик витрин выписана в шапке `sql/postgres/schema.sql`; имена колонок
в тестах — оттуда, а не из кода.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SQL_DIR = Path(__file__).resolve().parents[1] / "etl" / "sql"

_COMMENT_RE = re.compile(r"--[^\n]*")
_AS_RE = re.compile(r"(?i)\bAS\s+([A-Za-z_][A-Za-z0-9_]*)")
_AVG_RE = re.compile(r"(?i)\bavg(?:State)?\s*\(")
_TAIL_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _text(engine: str) -> str:
    """DDL движка без `--`-комментариев (в комментариях есть и имена колонок, и цифры)."""
    return _COMMENT_RE.sub("", (SQL_DIR / engine / "schema.sql").read_text(encoding="utf-8"))


def _statement(engine: str, header: str) -> str:
    """Один стейтмент: от заголовка до ближайшего `;`."""
    text = _text(engine)
    assert header in text, f"в схеме {engine} нет стейтмента «{header}»"
    start = text.index(header)
    return text[start:text.index(";", start)]


def _balanced(text: str, open_paren: int) -> str:
    """Содержимое скобки, открытой в позиции `open_paren`."""
    depth = 0
    for i in range(open_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:i]
    raise AssertionError(f"незакрытая скобка в позиции {open_paren}")


def _chunks(body: str) -> list[str]:
    """Разбить перечисление по запятым ВЕРХНЕГО уровня (скобки не режем)."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [c.strip() for c in out if c.strip()]


def _name(chunk: str) -> str:
    """Имя колонки выражения: последний `AS` верхнего уровня, иначе хвостовой идентификатор."""
    alias = None
    for m in _AS_RE.finditer(chunk):
        before = chunk[:m.start()]
        if before.count("(") == before.count(")"):
            alias = m.group(1)
    if alias:
        return alias
    tail = _TAIL_NAME_RE.search(chunk)
    assert tail, f"не разобрано имя колонки: {chunk!r}"
    return tail.group(1)


def _select_columns(engine: str, header: str) -> tuple[str, ...]:
    """Имена колонок, которые отдаёт витрина (`SELECT ... FROM`)."""
    stmt = _statement(engine, header)
    body = stmt[stmt.upper().index("SELECT") + len("SELECT"):]
    depth = 0
    for m in re.finditer(r"(?i)\bFROM\b|[()]", body):
        token = m.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            body = body[:m.start()]
            break
    return tuple(_name(c) for c in _chunks(body))


def _table_columns(engine: str, header: str) -> tuple[str, ...]:
    """Имена колонок в объявлении таблицы."""
    text = _text(engine)
    body = _balanced(text, text.index("(", text.index(header)))
    return tuple(chunk.split()[0] for chunk in _chunks(body))


def _nvarchar_len(engine: str, header: str, column: str) -> int:
    """Разрядность NVARCHAR-колонки в объявлении таблицы."""
    text = _text(engine)
    start = text.index(header)
    body = _balanced(text, text.index("(", start))
    for chunk in _chunks(body):
        parts = chunk.split()
        if parts[0] == column:
            m = re.match(r"(?i)NVARCHAR\((\d+)\)", parts[1])
            assert m, f"колонка {column} объявлена не как NVARCHAR: {chunk!r}"
            return int(m.group(1))
    raise AssertionError(f"колонка {column} не найдена в {header}")


# ─────────────────── ключ вакансии в MS SQL (регрессия 09.08.2026) ───────────────────
# Ключ был NVARCHAR(80): himalayas и arbeitnow неймспейсят id слагом вакансии, в срезе
# 732 id длиннее 80 символов, самый длинный — 144. MS SQL на таком INSERT отвечает 8152
# и не грузит НИЧЕГО, а с выключенными ANSI_WARNINGS усечение схлопнуло бы разные id
# в один и уронило PRIMARY KEY. Резать id нельзя — это идентичность записи.
LONGEST_VACANCY_ID = 144   # замер по кешу родителя на 09.08.2026
VACANCY_KEY_LEN = 200      # объявленная разрядность ключа: 144 + запас
MSSQL_INDEX_KEY_LIMIT_BYTES = 1700


@pytest.mark.parametrize(("table", "column"), [
    ("CREATE TABLE staging.stg_vacancies", "id"),
    ("CREATE TABLE staging.stg_skills", "vacancy_id"),
    ("CREATE TABLE core.vacancies", "id"),
    ("CREATE TABLE core.vacancy_skills", "vacancy_id"),
])
def test_mssql_vacancy_key_is_wide_enough_for_namespaced_ids(table, column):
    assert _nvarchar_len("mssql", table, column) == VACANCY_KEY_LEN


def test_mssql_vacancy_key_covers_the_longest_id_in_the_cache():
    assert VACANCY_KEY_LEN > LONGEST_VACANCY_ID


def test_mssql_composite_key_fits_index_limit():
    # core.vacancy_skills: NVARCHAR(200) (2 байта на символ) + INT
    assert VACANCY_KEY_LEN * 2 + 4 <= MSSQL_INDEX_KEY_LIMIT_BYTES


# ─────────────────── зарплатные метрики: единица измерения — рубль ───────────────────
# До 09.08.2026 все витрины усредняли salary_min/salary_max по 45 кодам валют без
# конверсии: доллары, узбекские сумы и вьетнамские донги складывались в один avg.

@pytest.mark.parametrize("engine", ["postgres", "mssql", "clickhouse"])
def test_only_rouble_salary_columns_are_averaged(engine):
    averaged: set[str] = set()
    text = _text(engine)
    for m in _AVG_RE.finditer(text):
        averaged |= set(re.findall(r"salary_\w+", _balanced(text, m.end() - 1)))
    assert averaged == {"salary_min_rub", "salary_max_rub"}


CITY_STATS = ("city", "vacancies", "with_salary", "with_salary_rub",
              "avg_salary_min_rub", "avg_salary_max_rub", "remote_share_pct")
SOURCE_STATS = ("source", "vacancies", "with_salary", "with_salary_rub",
                "avg_salary_min_rub", "avg_salary_max_rub", "remote_share_pct")
SALARY_BY_EXPERIENCE = ("experience", "vacancies", "with_salary_rub",
                        "avg_salary_min_rub", "avg_salary_max_rub")
SKILL_DEMAND = ("skill", "vacancies")
TOP_EMPLOYERS = ("employer", "vacancies")


@pytest.mark.parametrize(("engine", "header", "expected"), [
    ("postgres", "CREATE MATERIALIZED VIEW mart.city_stats", CITY_STATS),
    ("postgres", "CREATE MATERIALIZED VIEW mart.source_stats", SOURCE_STATS),
    ("postgres", "CREATE MATERIALIZED VIEW mart.salary_by_experience", SALARY_BY_EXPERIENCE),
    ("postgres", "CREATE MATERIALIZED VIEW mart.skill_demand", SKILL_DEMAND),
    ("postgres", "CREATE MATERIALIZED VIEW mart.top_employers", TOP_EMPLOYERS),
    ("mssql", "CREATE OR ALTER VIEW mart.city_stats", CITY_STATS),
    ("mssql", "CREATE OR ALTER VIEW mart.source_stats", SOURCE_STATS),
    ("mssql", "CREATE OR ALTER VIEW mart.salary_by_experience", SALARY_BY_EXPERIENCE),
    ("mssql", "CREATE OR ALTER VIEW mart.skill_demand", SKILL_DEMAND),
    ("mssql", "CREATE OR ALTER VIEW mart.top_employers", TOP_EMPLOYERS),
])
def test_mart_exposes_specified_columns(engine, header, expected):
    assert _select_columns(engine, header) == expected


# ClickHouse хранит состояния агрегатов, поэтому имена короче, но единица та же — рубль.
CH_SALARY_BY_EXP = ("exp_bucket", "avg_min_rub", "avg_max_rub", "vacancies", "with_salary_rub")
CH_SOURCE_STATS = ("source", "vacancies", "avg_min_rub", "avg_max_rub", "remote", "with_salary_rub")


@pytest.mark.parametrize(("table_header", "mv_header", "expected"), [
    ("CREATE TABLE IF NOT EXISTS hh.salary_by_exp",
     "CREATE MATERIALIZED VIEW hh.salary_by_exp_mv", CH_SALARY_BY_EXP),
    ("CREATE TABLE IF NOT EXISTS hh.source_stats",
     "CREATE MATERIALIZED VIEW hh.source_stats_mv", CH_SOURCE_STATS),
])
def test_clickhouse_mart_table_and_mv_expose_specified_columns(table_header, mv_header, expected):
    # MV с `TO` пишет в приёмник по ИМЕНАМ колонок — разъехавшись, вставка упадёт
    assert _table_columns("clickhouse", table_header) == expected
    assert _select_columns("clickhouse", mv_header) == expected


def test_mssql_rounds_averages_instead_of_truncating():
    # 08.08.2026 в родителе: усечение давало «4166 там, где верно 4167». T-SQL
    # CAST(decimal AS INT) отбрасывает дробную часть, PG и CH округляют.
    text = _text("mssql")
    assert text.count("CAST(AVG(") == 0
    assert text.count("CAST(ROUND(AVG(") == 6   # три витрины x две метрики


# ─────────────────── бакет «не указан»: пусто ИЛИ NULL ───────────────────
# 20 650 вакансий (19 % среза) уходили в бакет с ПУСТОЙ подписью: родитель кодирует
# «опыт не указан» пустой строкой, а `coalesce(experience, ...)` реагирует только на NULL.
# Домен с 09.08.2026 отдаёт None, но витрина обязана держать оба представления.

@pytest.mark.parametrize(("engine", "expression", "occurrences"), [
    ("postgres", "coalesce(nullif(v.experience, ''), 'не указан')", 2),
    ("mssql", "COALESCE(NULLIF(v.experience, N''), N'не указан')", 2),
    ("clickhouse", "coalesce(nullIf(experience, ''), 'не указан')", 1),
])
def test_experience_bucket_covers_empty_string_and_null(engine, expression, occurrences):
    # PG/MSSQL — одно и то же выражение в SELECT и в GROUP BY, CH группирует по алиасу
    assert _text(engine).count(expression) == occurrences


# ─────────────────── правка витрины доезжает до движка ───────────────────
# `CREATE ... IF NOT EXISTS` на поднятой БД молча оставляет СТАРОЕ определение: в MS SQL
# (CREATE OR ALTER VIEW) правка применялась сразу, в PG и CH — никогда, и движки
# расходились бы навсегда при зелёной проверке идемпотентности.

@pytest.mark.parametrize(("engine", "drop_statement", "marts"), [
    ("postgres", "DROP MATERIALIZED VIEW IF EXISTS", 5),
    ("clickhouse", "DROP VIEW IF EXISTS", 3),
])
def test_mart_definitions_are_recreated_on_every_init(engine, drop_statement, marts):
    text = _text(engine)
    assert text.count(drop_statement) == marts
    assert text.count("CREATE MATERIALIZED VIEW IF NOT EXISTS") == 0
