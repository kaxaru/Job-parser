"""Чёрные списки переопределяются профилем, а не правкой кода.

Это предпочтения СОИСКАТЕЛЯ: «не беру QA» и «беру только QA» — одинаково законные
настройки. До 07.08.2026 все восемь регексов были вшиты в `candidates.py`, и владельцу
чужого форка пришлось бы править исходник, а его правки конфликтовали бы с апстримом
при каждом обновлении.

Проверяется сам механизм `_rx` — он читает уже загруженный `config.APPLY_BLACKLISTS`,
поэтому здесь подменяется словарь, а не файл на диске (config читает профиль на импорте).
"""

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


@pytest.mark.parametrize("title, expected", [
    ("QA Engineer", True),
    ("Инженер по тестированию", False),      # слова «тестирован*» в списке нет
    ("Аква-дизайнер", False),                # «qa» внутри слова не считается совпадением
    ("Python Developer", False),
])
def test_word_list_matches_whole_words(title, expected, blacklists):
    """Основной способ задать правило — СПИСОК СЛОВ: регулярку ради своих предпочтений
    человек писать не должен (08.08.2026). Слово матчится целиком."""
    blacklists({"qa": ["QA", "SDET"]})
    assert bool(C._rx("qa", _DEFAULT).search(title)) is expected


@pytest.mark.parametrize("title, expected", [
    ("Тестировщик ПО", True),
    ("Вакансия для тестировщика", True),     # падеж ловится тем же префиксом
    ("Тестирование не требуется", False),    # другой корень — «тестирован», не «тестировщ»
])
def test_star_suffix_matches_by_prefix(title, expected, blacklists):
    # Хвост `*` — под русскую морфологию: без него пришлось бы перечислять все падежи.
    blacklists({"qa": ["тестировщ*"]})
    assert bool(C._rx("qa", _DEFAULT).search(title)) is expected


@pytest.mark.parametrize("word, title", [
    ("C#", "Разработчик C#"),
    (".NET", "Программист .NET"),
    ("1С", "Программист 1С"),
])
def test_words_with_punctuation_still_match(word, title, blacklists):
    """`\\b` требует буквенно-цифрового соседа, поэтому у `C#` хвостовая граница не работает
    и её ставить нельзя — иначе правило молча не срабатывало бы никогда."""
    blacklists({"other_lang": [word]})
    assert bool(C._rx("other_lang", _DEFAULT).search(title)) is True


def test_word_list_is_case_insensitive(blacklists):
    blacklists({"qa": ["Тестировщик"]})
    assert bool(C._rx("qa", _DEFAULT).search("ТЕСТИРОВЩИК Python")) is True


def test_empty_word_list_disables_the_rule(blacklists):
    # Тот же контракт, что у пустой строки: «правило мне не нужно».
    blacklists({"qa": []})
    assert bool(C._rx("qa", _DEFAULT).search("QA Engineer")) is False


def test_regex_metacharacters_in_a_word_are_literal(blacklists):
    """Список — это СЛОВА, а не паттерны: точка не должна работать как «любой символ»,
    иначе безобидное «Node.js» в списке начало бы ловить «NodeXjs» и соседей."""
    blacklists({"qa": ["Node.js"]})
    rx = C._rx("qa", _DEFAULT)
    assert bool(rx.search("Node.js разработчик")) is True
    assert bool(rx.search("NodeXjs разработчик")) is False


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
    wired = {"senior", "internship", "management", "non_engineering", "qa", "analyst", "ml",
             "devops", "other_lang", "target_engineering"}
    assert keys == wired, f"в примере лишние/недостающие ключи: {keys ^ wired}"


def test_example_rules_all_compile(blacklists):
    """Пример — то, что скопирует следующий пользователь: битое правило там означает
    предупреждение в лог и тихую подмену на дефолт при первом же запуске. Компилируем
    ТЕМ ЖЕ путём, что и приложение: правило может быть и списком слов, и строкой-регексом."""
    import json
    from pathlib import Path
    example = json.loads(
        (Path(__file__).parents[3] / "resume_profile.example.json").read_text(encoding="utf-8"))
    rules = {k: v for k, v in (example.get("blacklists") or {}).items()
             if not k.startswith("_")}
    blacklists(rules)
    for key in rules:
        # дефолт-заглушка, которая не совпадает ни с чем: если правило битое и произошёл
        # откат, тест это увидит по пустому результату ниже
        rx = C._rx(key, r"(?!)")
        assert rx.pattern != r"(?!)", f"правило {key} не скомпилировалось и упало в дефолт"
