"""VO грейда — один словарь тайтлов на чаты и анкеты.

АУДИТ 07.08.2026: существовали ДВА независимых `Grade` — в `chat_answer.py` и `form_fill.py`,
и их лексиконы разошлись. «Тимлид» и «архитектор» знала только анкетная копия, «старший»,
`Sr`, `Staff`, «принципал» — только чатовая. Одна и та же вакансия получала разный грейд в
зависимости от того, кто спрашивает, а от грейда зависит НАЗЫВАЕМАЯ РАБОТОДАТЕЛЮ СУММА
(`salary_by_grade`). Оба пути необратимы.

Тесты ниже перечисляют ярлыки ОБЕИХ прежних копий: каждый обязан работать в объединённом
словаре, иначе слияние что-то потеряло.
"""
import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.grade import Grade


@pytest.mark.parametrize("title", [
    # знала только чатовая копия
    "Старший Python-разработчик", "Sr Python Engineer", "Staff Engineer", "Принципал",
    "Ведущий backend", "Senior Data Engineer", "Сеньор", "Python Team Lead",
    # знала только анкетная
    "Тимлид Python", "Тим-лид разработки", "Архитектор решений", "Principal Engineer",
])
def test_senior_titles_from_both_former_copies(title):
    assert Grade.from_title(title) is Grade.SENIOR


@pytest.mark.parametrize("title", [
    "Junior Python", "Джуниор", "Младший разработчик", "Стажёр", "Стажер",
    "Jr Developer", "Ученик",          # знала только чатовая
    "Интерн", "Intern Python", "Trainee",   # знала только анкетная
])
def test_junior_titles_from_both_former_copies(title):
    assert Grade.from_title(title) is Grade.JUNIOR


@pytest.mark.parametrize("title", ["Middle Python", "Мидл разработчик", "Средний уровень"])
def test_middle_titles(title):
    assert Grade.from_title(title) is Grade.MIDDLE


def test_title_without_grade_is_none():
    # грейда нет в тайтле у 74 % вакансий — это норма, а не ошибка
    assert Grade.from_title("Python Developer") is None
    assert Grade.from_title("") is None


def test_senior_wins_over_junior_in_the_same_title():
    # порядок проверки значим: «Senior … для junior-команды» — senior-позиция
    assert Grade.from_title("Senior Java для junior-команды") is Grade.SENIOR


# ── фолбэк по вилке опыта ──

@pytest.mark.parametrize("exp, expected", [
    (Experience.NONE, Grade.JUNIOR),
    (Experience.BETWEEN_1_3, Grade.JUNIOR),   # решение владельца профиля (20.07.2026)
    (Experience.BETWEEN_3_6, Grade.MIDDLE),
    (Experience.MORE_6, Grade.SENIOR),
    (None, None),
])
def test_from_experience(exp, expected):
    assert Grade.from_experience(exp) is expected


def test_title_beats_the_experience_field():
    # «Senior …» с вилкой «1–3 года» — всё равно senior
    assert Grade.from_vacancy("Senior Python", "between1And3") is Grade.SENIOR


def test_experience_used_when_title_is_silent():
    assert Grade.from_vacancy("Python Developer", "moreThan6") is Grade.SENIOR


def test_nothing_known_is_none():
    """Домен НЕ ставит дефолт: цена ошибки у вызывающих разная. В чате промолчать дешевле,
    чем назвать вилку наугад; в анкете поле обязано быть заполнено, иначе вакансия уходит
    человеку. Выбор дефолта остаётся за ними и подписан на месте."""
    assert Grade.from_vacancy("Python Developer", None) is None
    assert Grade.from_vacancy("Python Developer", "мусор") is None


def test_codes_are_the_salary_by_grade_keys():
    # wire-формат: значения = ключи resume_profile.json::answers.salary_by_grade
    assert [g.code for g in Grade] == ["junior", "middle", "senior"]
