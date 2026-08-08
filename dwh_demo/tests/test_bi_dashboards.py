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
    "Зарплата по опыту · валюта портала (суммы не сравнимы)",
    "Топ-15 работодателей",
    "Города (топ-15): вакансии, з/п, «Удалённо или гибрид» · валюта портала (суммы не сравнимы)",
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
    "Зарплата по опыту · Postgres · валюта портала (суммы не сравнимы)",
    "Зарплата по опыту · ClickHouse (avgMerge) · валюта портала (суммы не сравнимы)",
    "Топ-15 работодателей · Postgres",
    "Топ-15 работодателей · ClickHouse",
    "Города · Postgres · валюта портала (суммы не сравнимы)",
    "Города · ClickHouse · валюта портала (суммы не сравнимы)",
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
    "Зарплата по опыту · валюта портала (суммы не сравнимы)",
    "Топ-15 работодателей",
    "Города (топ-15) · валюта портала (суммы не сравнимы)",
]

SOURCES_CARDS = [
    "Вакансий по источникам",
    "Медиана нижней и верхней границы з/п по источникам · валюта портала (суммы не сравнимы)",
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
