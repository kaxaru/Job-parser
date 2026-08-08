"""Подписи карточек дашбордов — контракт с человеком, который видит только график.

Аудит 09.08.2026 нашёл на дашбордах тот же класс дефекта, что «Автобан %» у родителя:
подпись обещает одно, метрика считает другое. Два случая:
  * «Удалённые вакансии» — а витрина считает `is_remote` = remote + flexible (гибрид тоже);
  * «Медиана зарплатной вилки по источникам» — а столбики стоят в РАЗНЫХ валютах
    (в кеше 45 кодов), то есть сравниваются единицы измерения, а не деньги.
Здесь зафиксированы ИМЕНА карточек литералами: молчаливый откат подписи к «Удалённым»
или пропажа пометки о валюте делают тест красным.

Дашборд строится на фейковом фасаде — HTTP и Metabase не нужны (это закрывает пробел
docs/testing.md «bi/dashboards/* не тестируются»).
"""
import inspect
import re
from pathlib import Path

import pytest

from bi.registry import REGISTRY

pytestmark = pytest.mark.unit


class FakeMetabase:
    """Фасад Metabase без HTTP: запоминает имена созданных карточек в порядке создания."""

    def __init__(self):
        self.card_names: list[str] = []
        self.dashboard_titles: list[str] = []

    def find_database(self, name, engine):
        return 1

    def create_card(self, name, db_id, sql, display, viz=None, tags=None):
        self.card_names.append(name)
        return len(self.card_names)

    def run_sql(self, db_id, sql):
        return []

    def upsert_dashboard(self, title):
        self.dashboard_titles.append(title)
        return 100

    def set_dashboard(self, d_id, dashcards, parameters=None):
        return None


OVERVIEW_CARDS = [
    "Всего вакансий",
    "Удалённо или гибрид",
    "С указанной зарплатой",
    "Спрос на навыки (топ-15)",
    "Зарплата по опыту · ₽ (пересчёт по курсу)",
    "Топ-15 работодателей",
    "Города (топ-15): вакансии, з/п, «Удалённо или гибрид» · ₽ (пересчёт по курсу)",
]

COMPARISON_CARDS = [
    "Всего вакансий · Postgres",
    "Всего вакансий · ClickHouse",
    "Удалённо или гибрид · Postgres",
    "Удалённо или гибрид · ClickHouse",
    "С зарплатой · Postgres",
    "С зарплатой · ClickHouse",
    "Спрос на навыки · Postgres (mart REFRESH)",
    "Спрос на навыки · ClickHouse (AggregatingMergeTree)",
    "Зарплата по опыту · Postgres · ₽ (пересчёт по курсу)",
    "Зарплата по опыту · ClickHouse (avgMerge) · ₽ (пересчёт по курсу)",
    "Топ-15 работодателей · Postgres",
    "Топ-15 работодателей · ClickHouse",
    "Города · Postgres · ₽ (пересчёт по курсу)",
    "Города · ClickHouse · ₽ (пересчёт по курсу)",
]

COOCCURRENCE_CARDS = [
    "Всего вакансий с навыком",
    "Что ищут ВМЕСТЕ с выбранным навыком",
]

MSSQL_CARDS = [
    "Всего вакансий",
    "Удалённо или гибрид",
    "С зарплатой",
    "Спрос на навыки (топ-15)",
    "Зарплата по опыту · ₽ (пересчёт по курсу)",
    "Топ-15 работодателей",
    "Города (топ-15) · ₽ (пересчёт по курсу)",
]

SOURCES_CARDS = [
    "Вакансий по источникам",
    "Средняя нижняя и верхняя граница з/п по источникам · ₽ (пересчёт по курсу)",
    "Доля «Удалённо или гибрид» по источникам, %",
    "Покрытие зарплатой по источникам",
    "Топ-12 навыков рынка — в разрезе источника",
]


@pytest.mark.parametrize("key, expected", [
    ("overview", OVERVIEW_CARDS),
    ("comparison", COMPARISON_CARDS),
    ("cooccurrence", COOCCURRENCE_CARDS),
    ("mssql", MSSQL_CARDS),
    ("sources", SOURCES_CARDS),
])
def test_card_captions_name_what_the_metric_counts(key, expected):
    client = FakeMetabase()
    REGISTRY[key]().build(client)
    assert client.card_names == expected


@pytest.mark.parametrize("key, title", [
    # Заголовок — ключ идемпотентности `client.upsert_dashboard`: его дрейф создаёт
    # ВТОРОЙ дашборд вместо обновления первого, поэтому он зафиксирован литералом.
    ("overview", "HH — рынок труда (Python / Data Engineer)"),
    ("comparison", "HH — Postgres vs ClickHouse (один BI, два движка)"),
    ("cooccurrence", "HH — стек рядом с языком (co-occurrence)"),
    ("mssql", "HH — MS SQL (T-SQL DWH)"),
    # 09.08.2026: было «Источники — hh vs hirify vs talanto vs getmatch» — 4 портала
    # в заголовке при 9 столбиках на графике.
    ("sources", "Источники — сравнение порталов"),
])
def test_dashboard_title_is_the_idempotency_key(key, title):
    client = FakeMetabase()
    REGISTRY[key]().build(client)
    assert client.dashboard_titles == [title]


# ─── Колонки карточек существуют в витринах (инцидент 09.08.2026) ────────────────
# Схемы переименовали зарплатные метрики в `*_rub`, а SQL карточек остался на старых
# именах: шесть карточек вернули бы «колонки нет» на живом Metabase. Юниты этого не
# ловили — страж выше проверяет ПОДПИСИ, а SQL исполняется только против БД.
# Здесь тот же вопрос решается статически: каждое зарплатное имя, которое карточка
# селектит, обязано встречаться хотя бы в одной schema.sql.
_SALARY_COL = re.compile(r"\b(?:avg|median|with)_salary\w*|\bavg_(?:min|max)\w*")
_SCHEMA_TEXT = "\n".join(
    p.read_text(encoding="utf-8")
    for p in (Path(__file__).resolve().parents[1] / "etl" / "sql").rglob("schema.sql"))


def _salary_columns(key: str) -> set[str]:
    """Зарплатные имена, которые селектит модуль дашборда (SQL живёт в нём строками)."""
    src = Path(inspect.getfile(REGISTRY[key])).read_text(encoding="utf-8")
    return set(_SALARY_COL.findall(src))


@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_every_salary_column_a_card_selects_exists_in_some_mart(key):
    missing = sorted(c for c in _salary_columns(key) if c not in _SCHEMA_TEXT)
    assert missing == []
