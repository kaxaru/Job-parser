"""Личные настройки живут в `resume_profile.json`, а не в исходниках.

Владелец форка должен настраивать систему под себя правкой ОДНОГО файла: его правки в
`config.py` конфликтовали бы при каждом обновлении из апстрима. До 08.08.2026 в коде
буквально стояли город владельца (`APPLY_OFFICE_CITIES`), его поисковые запросы и тексты
писем ленты.

Механизм `_profile_list` читает уже загруженный `config._rp` (профиль читается на импорте
модуля), поэтому здесь подменяется словарь, а не файл на диске.
"""
import json
from pathlib import Path

import pytest

from hrwork import config as C

_EXAMPLE = Path(__file__).parents[2] / "resume_profile.example.json"


@pytest.fixture
def profile(monkeypatch):
    """Подменить загруженный профиль на время теста."""
    def _set(mapping):
        monkeypatch.setattr(C, "_rp", mapping)
        return mapping
    return _set


def test_key_absent_uses_the_default(profile):
    profile({})
    assert C._profile_list("office_cities", ["Москва"]) == ["Москва"]


def test_profile_value_wins_over_the_default(profile):
    profile({"office_cities": ["Казань", "Уфа"]})
    assert C._profile_list("office_cities", ["Москва"]) == ["Казань", "Уфа"]


def test_empty_list_disables_the_setting(profile):
    """Пустой список = «эта настройка мне не нужна», и в дефолт он НЕ проваливается.
    Тот же контракт, что у `blacklists` с пустой строкой. Иначе «офис мне не нужен»
    (`"office_cities": []`) молча вернуло бы город прошлого владельца профиля."""
    profile({"office_cities": []})
    assert C._profile_list("office_cities", ["Москва"]) == []


def test_values_are_coerced_to_strings(profile):
    # area id в JSON человек может написать числом — сравнение с городом вакансии строковое
    profile({"office_cities": [1, "Москва"]})
    assert C._profile_list("office_cities", []) == ["1", "Москва"]


def test_wrong_type_falls_back_to_the_default(profile):
    """Профиль пишет человек: строка вместо списка это опечатка, а не настройка.
    Без проверки типа `[str(x) for x in "Москва"]` дал бы список букв."""
    profile({"office_cities": "Москва"})
    assert C._profile_list("office_cities", ["Самара"]) == ["Самара"]


def test_empty_list_falls_back_when_emptiness_is_meaningless(profile):
    """Второй контракт, выбираемый явно: для `search_queries` пустой список — не «настройка
    снята», а сломанный конфиг, который обнулил бы сбор целиком."""
    profile({"search_queries": []})
    assert C._profile_list_or_default("search_queries", ["python"]) == ["python"]


def test_explicit_values_win_in_both_contracts(profile):
    profile({"search_queries": ["go разработчик"]})
    assert C._profile_list_or_default("search_queries", ["python"]) == ["go разработчик"]


def test_repo_defaults_for_search_are_intact():
    """Дефолты сбора после выноса в профиль не сдвинулись. Сверяется САМ дефолт, а не
    действующее значение: у форка с ключами в профиле оно законно другое."""
    assert len(C._SEARCH_QUERIES_DEFAULT) == 35
    assert len(C._CITIES_DEFAULT) == 34
    assert C._CITIES_DEFAULT["1"] == "Москва"


@pytest.mark.parametrize("actual, expected", [
    ("_CORE_WIDE_DEFAULT", ["Django", "Flask", "PostgreSQL", "MySQL", "Redis", "Kafka"]),
    ("_OFFICE_CITIES_DEFAULT", ["Москва", "Санкт-Петербург", "Тольятти", "Самара"]),
    ("_EXTRA_EXP_IDS_DEFAULT", ["between3And6"]),
])
def test_tier_defaults_unchanged(actual, expected):
    """Дефолты тиров — прежнее поведение владельца репозитория: вынос в профиль не должен
    был ничего сдвинуть у того, кто этих ключей не писал. Сверяется САМ дефолт, а не
    действующее значение: у форка с ключами в профиле оно законно другое."""
    assert getattr(C, actual) == expected


def test_example_profile_is_valid_json():
    """Пример — то, что скопирует следующий пользователь. Битый JSON там означает, что
    система молча уедет на дефолты: config глотает JSONDecodeError."""
    json.loads(_EXAMPLE.read_text(encoding="utf-8"))


def test_every_example_key_is_actually_read():
    """Каждый ключ примера обязан кем-то читаться, иначе документация обещает ручку,
    которой нет. Так в примере жил `answers.form_answers`: движок читает form_answers
    ТОЛЬКО с верхнего уровня, и вложенная копия не делала ничего (найдено 08.08.2026)."""
    example = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    keys = {k for k in example if not k.startswith("_")}
    wired = {
        "core", "exp_ids",              # config: RESUME_CORE / RESUME_EXP_IDS
        "answers",                      # chat_answer.py, form_fill.py::_age
        "form_answers",                 # form_fill.py::form_answers
        "blacklists",                   # config: APPLY_BLACKLISTS -> candidates._rx
        "cover_template",               # cover.py (крон-отклики)
        "feed_cover_templates",         # feed.py -> FEED_COVER_TEMPLATES_PY -> cover.js
        "core_wide", "office_cities",   # config: тиры отбора 2 и 3
        "extra_exp_ids",                # config: APPLY_EXTRA_EXP_IDS
        "search_queries", "cities",     # config: что и где собираем на hh
    }
    assert keys == wired, f"в примере лишние/недостающие ключи: {sorted(keys ^ wired)}"


def test_example_answers_keys_are_actually_read():
    """То же для вложенного блока `answers` — именно там появился мёртвый ключ."""
    example = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    keys = {k for k in example["answers"] if not k.startswith("_")}
    wired = {
        "stack", "stack_past", "years_text", "years_python_text", "format_text",
        "english_text", "education_text", "citizenship_text", "salary_by_grade",
        "office_city", "office_city_en", "years_text_en", "years_python_text_en",
        "format_text_en", "english_text_en", "education_text_en", "citizenship_text_en",
        "answer_negative", "practices", "birth_date",
    }
    assert keys == wired, f"в примере лишние/недостающие ключи: {sorted(keys ^ wired)}"
