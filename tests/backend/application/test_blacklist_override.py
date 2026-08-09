"""Чёрные списки переопределяются профилем, а не правкой кода.

Это предпочтения СОИСКАТЕЛЯ: «не беру QA» и «беру только QA» — одинаково законные
настройки. До 07.08.2026 все восемь регексов были вшиты в `candidates.py`, и владельцу
чужого форка пришлось бы править исходник, а его правки конфликтовали бы с апстримом
при каждом обновлении.

Проверяется сам механизм `_rx` — он читает уже загруженный `config.APPLY_BLACKLISTS`,
поэтому здесь подменяется словарь, а не файл на диске (config читает профиль на импорте).
"""

import json
from pathlib import Path

import pytest

from hrwork.application.apply import candidates as C

_DEFAULT = r"\bqa\b|тестировщ"

_EXAMPLE = Path(__file__).parents[3] / "resume_profile.example.json"
# Правила примера читаются на уровне модуля — чтобы параметризовать по КЛЮЧАМ (имя ключа
# попадает в отчёт), а не перебирать их циклом внутри одного теста.
_EXAMPLE_RULES = {k: v for k, v in json.loads(
    _EXAMPLE.read_text(encoding="utf-8"))["blacklists"].items() if not k.startswith("_")}
# Дефолт-заглушка для `_rx`: ловит ТОЛЬКО эту строку и ничего больше. Если правило примера
# не скомпилировалось, `_rx` вернёт именно её — и это видно по ПОВЕДЕНИЮ регекса,
# без заглядывания в `.pattern` (аудит 09.08.2026).
_FALLBACK_PROBE = "zzz_fallback_probe_zzz"


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
    `_help_qa` попал бы в словарь и молча ничего не сделал.

    АУДИТ 09.08.2026: страж читал ЖИВОЙ config и на профиле без блока `blacklists` (как у
    владельца репозитория сейчас) проходил вхолостую — пустой словарь `_`-ключей не
    содержит по определению. Поэтому рядом стоит КОНТРОЛЬ: пояснения в примере есть,
    значит фильтру действительно есть что отсеивать у форка, который пример скопировал."""
    from hrwork.config import APPLY_BLACKLISTS
    example_bl = json.loads(_EXAMPLE.read_text(encoding="utf-8"))["blacklists"]
    assert sorted(k for k in example_bl if k.startswith("_")) == [
        "_help_analyst", "_help_devops", "_help_internship", "_help_management", "_help_ml",
        "_help_non_engineering", "_help_other_engineering", "_help_other_lang",
        "_help_qa", "_help_senior",
        "_help_target_engineering"]
    assert [k for k in APPLY_BLACKLISTS if k.startswith("_")] == []


def test_every_rule_key_is_wired():
    """Каждый ключ из примера профиля обязан что-то переопределять. Иначе документация
    обещает ручку, которой нет."""
    # ключи, которые реально читает candidates.py
    wired = {"senior", "internship", "other_engineering", "management", "non_engineering",
             "qa", "analyst", "ml", "devops", "other_lang", "target_engineering"}
    assert set(_EXAMPLE_RULES) == wired


@pytest.mark.parametrize("key", sorted(_EXAMPLE_RULES))
def test_example_rule_compiles_and_does_not_fall_back(key, blacklists):
    """Пример — то, что скопирует следующий пользователь: битое правило там означает
    предупреждение в лог и тихую подмену на дефолт при первом же запуске.

    АУДИТ 09.08.2026: раньше это был единственный `for`-перебор случаев во всей зоне
    (падение на первом ключе скрывало остальные), и утверждал он о ВНУТРЕННЕЙ структуре —
    строке `rx.pattern`. Теперь ключ называется в отчёте, а откат виден по ПОВЕДЕНИЮ:
    правило, упавшее в дефолт-заглушку, поймало бы её строку-зонд."""
    blacklists(_EXAMPLE_RULES)
    assert bool(C._rx(key, _FALLBACK_PROBE).search(_FALLBACK_PROBE)) is False
