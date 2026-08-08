"""Словарь ответов (`resume_profile.json::form_answers`) — данные, а не код, но именно они
уходят работодателю, поэтому регексы записей проверяются как поведение.

РЕГРЕССИЯ 27.07: в записи про офисный формат ветка `очно` стояла без границ слова и матчилась
внутри других слов — «нагруз(очно)м», «нет(очно)й». Из-за этого на «Подтвердите, что всё в вашем
резюме — правда» анкета отвечала «Нет», а вопрос про нагрузочное тестирование получал офисный
отказ вместо своего ответа. Границы слова обязательны.

Здесь ДВА разных предмета, и их нельзя мерить одним условием пропуска:

* семантика движка (`match_answer`: границы слова, membership по опциям) — от профиля
  не зависит и проверяется ВСЕГДА, на литеральном словаре;
* личный словарь владельца — данные, которых в репозитории нет, поэтому такой набор
  пропускается, если личного профиля не подложено.

Инцидент 08.08.2026: пропуск был завязан только на `not F.form_answers()`, а фолбэк-пример
подкладывала session-фикстура — то есть ПОСЛЕ сборки модулей, когда `pytestmark` уже
вычислен. На CI профиля нет, набор молча пропускался, и это выглядело как «зелено».
Перенос фолбэка в `pytest_configure` (находка 63) убрал опоздание — пример стал доступен
к моменту импорта, набор запустился на ЧУЖОМ словаре и упал. Условие пропуска обязано
спрашивать «профиль ЛИЧНЫЙ?», а не «профиль ЕСТЬ?».
"""
from pathlib import Path
from typing import Any

import pytest

from hrwork.application.apply.forms import form_fill as F

_ROOT = Path(__file__).resolve().parents[3]


def _profile_is_personal() -> bool:
    """Личный профиль, а не подложенный корневым conftest'ом `resume_profile.example.json`."""
    real, example = _ROOT / "resume_profile.json", _ROOT / "resume_profile.example.json"
    if not real.exists():
        return False
    if not example.exists():
        return True
    return real.read_bytes() != example.read_bytes()


def _answer(prompt: str, options: tuple[str, ...]) -> str | None:
    found = F.match_answer(prompt, options)
    return found[0] if found else None


# ─── Семантика движка: literal-словарь, от профиля не зависит ─────────────────
# Регрессия 27.07 воспроизводится здесь и проверяется на ЛЮБОЙ машине, включая CI без
# профиля: ветка `очно` без границ слова матчилась внутри «нагруз(очно)м» и «нет(очно)й».

_OFFICE_RX = r"\bочно\b|\bв\s+офисе\b"
_ENGINE_DICT: list[dict[str, Any]] = [
    {"q": _OFFICE_RX, "a": "Нет"},
    {"q": r"нагрузочн\w*\s+тестирован", "a": "Да"},
]


@pytest.fixture()
def engine_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяем словарь целиком: предмет проверки — сопоставление, а не чьи-то данные."""
    monkeypatch.setattr(F, "form_answers", lambda: _ENGINE_DICT)


@pytest.mark.usefixtures("engine_dict")
@pytest.mark.parametrize(("prompt", "expected"), [
    ("Готовы ли вы работать очно в офисе?", "Нет"),
    ("Какой у вас опыт в нагрузочном тестировании?", "Да"),   # «нагруз(очно)м» — не офис
    ("Опишите нето+чно сформулированный опыт", None),         # «нет(очно)й» — тоже не офис
])
def test_word_boundaries_keep_ochno_out_of_other_words(prompt: str,
                                                       expected: str | None) -> None:
    assert _answer(prompt, ()) == expected


@pytest.mark.usefixtures("engine_dict")
def test_answer_is_dropped_when_it_is_not_among_the_form_options() -> None:
    # membership: подпись «Нет» обязана быть среди опций, иначе запись не применяется
    assert _answer("Готовы ли вы работать очно в офисе?", ("Да", "Возможно")) is None


# ─── Личный словарь владельца: без личного профиля пропускаем ─────────────────
personal = pytest.mark.skipif(
    not (_profile_is_personal() and F.form_answers()),
    reason="личного resume_profile.json нет (или подложен пример) — словарь владельца не проверяем",
)

_RESUME_TRUTH = ("В моём резюме есть неточная информация",
                 "В моём резюме только достоверная информация")
_INTERNSHIP = ("Да, условия устраивают, интересно",
               "Заинтересован только в прохождении интенсива, без дальнейшей стажировки")


@personal
@pytest.mark.parametrize("prompt, options, expected", [
    ("Подтвердите, что всё в вашем резюме — правда. Если информация окажется неточной, мы "
     "отдадим предпочтение другим кандидатам.", _RESUME_TRUTH,
     "В моём резюме только достоверная информация"),
    ("Ознакомились ли вы с этапами отбора и стажировки?", _INTERNSHIP,
     "Да, условия устраивают, интересно"),
    ("Готовы ли вы работать очно в офисе?", ("Да", "Нет"), "Нет"),
])
def test_dictionary_answers_question(prompt, options, expected):
    assert _answer(prompt, options) == expected


@personal
def test_office_refusal_does_not_capture_load_testing():
    # «нагрузочном» содержит «очно» — офисный отказ не должен подменять собой ответ по существу
    prompt = "Какой у вас опыт в нагрузочном тестировании? Какие инструменты использовали?"
    assert _answer(prompt, ()) != "Нет"
