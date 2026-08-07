"""Чёрные списки переопределяются профилем, а не правкой кода.

Это предпочтения СОИСКАТЕЛЯ: «не беру QA» и «беру только QA» — одинаково законные
настройки. До 07.08.2026 все восемь регексов были вшиты в `candidates.py`, и владельцу
чужого форка пришлось бы править исходник, а его правки конфликтовали бы с апстримом
при каждом обновлении.

Проверяется сам механизм `_rx` — он читает уже загруженный `config.APPLY_BLACKLISTS`,
поэтому здесь подменяется словарь, а не файл на диске (config читает профиль на импорте).
"""
import re

import pytest

from hrwork.application.apply import candidates as C

_DEFAULT = r"\bqa\b|тестировщ"


@pytest.fixture
def blacklists(monkeypatch):
    """Подменить словарь переопределений на время теста."""
    def _set(mapping):
        monkeypatch.setattr(C, "APPLY_BLACKLISTS", mapping)
        return mapping
    return _set


def test_key_absent_uses_the_default(blacklists):
    blacklists({})
    rx = C._rx("qa", _DEFAULT)
    assert bool(rx.search("QA Engineer")) is True
    assert bool(rx.search("Python Developer")) is False


def test_profile_value_wins_over_the_default(blacklists):
    blacklists({"qa": r"\bsdet\b"})
    rx = C._rx("qa", _DEFAULT)
    assert bool(rx.search("SDET")) is True
    assert bool(rx.search("QA Engineer")) is False      # дефолт больше не действует


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_value_disables_the_rule(value, blacklists):
    """Пустая строка = «правило мне не нужно». Ловушка: пустой ПАТТЕРН совпадает с любой
    строкой и отсеял бы всё подряд, поэтому подставляется `(?!)`, не матчащийся никогда."""
    blacklists({"qa": value})
    rx = C._rx("qa", _DEFAULT)
    assert bool(rx.search("QA Engineer")) is False
    assert bool(rx.search("")) is False
    assert bool(rx.search("что угодно")) is False


def test_broken_regex_falls_back_to_the_default(blacklists):
    # у владельца профиля нет способа отладить регекс; тихо отключить фильтр отбора хуже
    blacklists({"qa": "["})
    rx = C._rx("qa", _DEFAULT)
    assert bool(rx.search("QA Engineer")) is True


def test_override_is_case_insensitive(blacklists):
    blacklists({"qa": "тестировщик"})
    assert bool(C._rx("qa", _DEFAULT).search("ТЕСТИРОВЩИК Python")) is True


def test_comment_keys_are_not_rules():
    """В примере профиля рядом с каждым правилом лежит `_help_*` — пояснение для человека.
    Ключи с подчёркиванием отсеиваются при загрузке (`config.APPLY_BLACKLISTS`), иначе
    `_help_qa` попал бы в словарь и молча ничего не сделал."""
    from hrwork.config import APPLY_BLACKLISTS
    assert not [k for k in APPLY_BLACKLISTS if k.startswith("_")]


def test_every_rule_key_is_wired(blacklists):
    """Каждый ключ из примера профиля обязан что-то переопределять. Иначе документация
    обещает ручку, которой нет."""
    import json
    from pathlib import Path
    example = json.loads(
        (Path(__file__).parents[3] / "resume_profile.example.json").read_text(encoding="utf-8"))
    keys = {k for k in (example.get("blacklists") or {}) if not k.startswith("_")}
    # ключи, которые реально читает candidates.py
    wired = {"senior", "management", "non_engineering", "qa", "analyst", "ml",
             "devops", "other_lang", "target_engineering"}
    assert keys == wired, f"в примере лишние/недостающие ключи: {keys ^ wired}"


def test_example_regexes_all_compile():
    """Пример — то, что скопирует следующий пользователь: битый регекс там означает
    предупреждение в лог при первом же запуске."""
    import json
    from pathlib import Path
    example = json.loads(
        (Path(__file__).parents[3] / "resume_profile.example.json").read_text(encoding="utf-8"))
    for key, pattern in (example.get("blacklists") or {}).items():
        if key.startswith("_"):
            continue
        re.compile(pattern)          # бросит re.error, если шаблон битый
