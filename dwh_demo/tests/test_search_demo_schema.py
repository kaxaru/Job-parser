"""Схема витрины поиска — контракт с прод-путём `/api/search` родителя.

`search_demo/load.py` создаёт таблицу `search_demo.vacancies`, из которой читает
`hrwork/infrastructure/search.py`. Проверить это прогоном нельзя: загрузчик требует
живой Postgres и импортирует пакет `hrwork`, которого в окружении подпроекта нет
(CI ставит только `dwh_demo/requirements-dev.txt`). Поэтому схема читается из ИСХОДНИКА
через `ast` — без импорта, без БД и без побочных эффектов родительского конфига.

Сторожит два инцидента:
  * 09.08.2026 — `sal_mid` (валюта портала) использовался как одна шкала: топ страницы
    занимали 50 000 000 UZS, а фильтр «з/п от 200 000» выбрасывал все долларовые вилки.
    Ответ — отдельная рублёвая колонка `sal_mid_rub` и постраничный индекс по ней;
  * 09.08.2026 — вторая копия `strip_html` в загрузчике разошлась с `etl/domain.py`
    (не декодировала HTML-сущности), и в сниппет доезжало «R&amp;D».
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LOADER = Path(__file__).resolve().parents[1] / "search_demo" / "load.py"
_TREE = ast.parse(LOADER.read_text(encoding="utf-8"))


def _string_constants() -> list[str]:
    """Все строковые литералы модуля (соседние литералы парсер уже склеил)."""
    return [n.value for n in ast.walk(_TREE)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _ddl() -> str:
    for node in _TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "DDL" for t in node.targets):
            return node.value.value
    raise AssertionError("в search_demo/load.py нет константы DDL")


# Колонки в порядке объявления. Первые одиннадцать плюс `created_at` читает
# hrwork/infrastructure/search.py; `description` и `doc` — полнотекстовая часть.
EXPECTED_COLUMNS = [
    "id", "source", "name", "employer", "city", "experience", "schedule",
    "is_remote_like", "sal_from", "sal_to", "sal_mid", "currency", "sal_mid_rub",
    "url", "techs", "created_at", "description", "doc",
]

EXPECTED_INDEXES = {
    "ix_doc_gin": "USING GIN (doc)",
    "ix_name_trgm": "USING GIN (name gin_trgm_ops)",
    "ix_city": "(city)",
    "ix_salmid": "(sal_mid)",
    "ix_salmid_page": "(sal_mid DESC NULLS LAST, id)",
    "ix_salmidrub_page": "(sal_mid_rub DESC NULLS LAST, id)",
    "ix_created": "(created_at)",
    "ix_source": "(source)",
}


def test_ddl_declares_the_columns_the_search_page_reads():
    columns = re.findall(r"^ {4}(\w+)\s", _ddl(), re.M)
    assert columns == EXPECTED_COLUMNS


def test_insert_fills_every_column_except_the_generated_tsvector():
    """`doc` — GENERATED ALWAYS: он единственный, которого нет в INSERT."""
    insert = next(s for s in _string_constants() if s.startswith("INSERT INTO search_demo.vacancies"))
    listed = re.search(r"vacancies\s+\(([^)]*)\)", insert).group(1)
    assert [c.strip() for c in listed.split(",")] == EXPECTED_COLUMNS[:-1]


def test_every_index_names_the_expression_it_serves():
    found = {}
    for s in _string_constants():
        m = re.match(r"CREATE INDEX (\w+)\s+ON search_demo\.vacancies\s+(.+);$", s)
        if m:
            found[m.group(1)] = m.group(2)
    assert found == EXPECTED_INDEXES


def test_strip_html_is_not_duplicated_in_the_loader():
    """Разметку снимает ОДНА функция стенда — `etl/domain.py::strip_html`."""
    defined = [n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)]
    imported = [(n.module, a.name) for n in ast.walk(_TREE)
                if isinstance(n, ast.ImportFrom) for a in n.names]
    assert "strip_html" not in defined
    assert ("etl.domain", "strip_html") in imported
