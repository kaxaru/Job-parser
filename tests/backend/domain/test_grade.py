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

from hrwork.domain.experience import _GRADE_TO_EXP, Experience
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


# ── СТРАЖ СОГЛАСОВАННОСТИ двух смежных словарей (аудит 08.08.2026) ──
#
# Один и тот же ярлык грейда приходит двумя путями: из структурного поля портала
# (`Experience.from_grades` -> `Grade.from_experience`) и из тайтла (`Grade.from_title`).
# Пути обязаны либо совпадать, либо расходиться ОСОЗНАННО — от грейда зависит называемая
# работодателю сумма (`salary_by_grade`), и оба потребителя необратимы.
#
# Ожидаемые значения ниже — литералы по обеим спецификациям (`_GRADE_TO_EXP` описывает
# требования РАБОТОДАТЕЛЯ по шкале HH, `_BY_EXP` — самооценку владельца профиля), а не
# вычисление из тех же таблиц: иначе тест повторил бы реализацию и прошёл при любой её
# ошибке. Расхождение зафиксировано как ожидаемое и разобрано в `grade.py::_BY_EXP`.

@pytest.mark.parametrize("word, via_experience, via_title", [
    # совпадают — трогать нечего
    ("trainee",   Grade.JUNIOR, Grade.JUNIOR),
    ("intern",    Grade.JUNIOR, Grade.JUNIOR),
    ("junior",    Grade.JUNIOR, Grade.JUNIOR),
    ("lead",      Grade.SENIOR, Grade.SENIOR),
    ("principal", Grade.SENIOR, Grade.SENIOR),
    # РАСХОЖДЕНИЯ, намеренные: шкала работодателя против самооценки владельца профиля
    ("middle",    Grade.JUNIOR, Grade.MIDDLE),
    ("mid",       Grade.JUNIOR, Grade.MIDDLE),
    ("senior",    Grade.MIDDLE, Grade.SENIOR),
    # тайтл про такие слова молчит — это не противоречие, а отсутствие сигнала
    ("entry",     Grade.JUNIOR, None),
    ("midweight", Grade.JUNIOR, None),
    ("head",      Grade.SENIOR, None),
    ("manager",   Grade.SENIOR, None),
    ("director",  Grade.SENIOR, None),
])
def test_grade_label_roundtrip_matches_the_title_dictionary(word, via_experience, via_title):
    assert Grade.from_experience(Experience.from_grades([word])) is via_experience
    assert Grade.from_title(word) is via_title


def test_known_divergences_between_grade_dictionaries_are_exactly_three():
    """Набор расхождений «ярлык -> опыт -> грейд» против «то же слово в тайтле» закрыт.

    Страж дрейфа: новое слово в `_GRADE_TO_EXP` или новый паттерн в `_TITLE_RX` не должны
    молча добавить четвёртое расхождение — цена ошибки здесь измеряется в рублях оффера.
    Приватные таблицы импортируются намеренно (жанр «страж согласованности», как
    `test_role.py` с `config.ROLE_PATTERNS`): перечислить слова литералом мало — тогда
    добавленный ключ не попал бы в проверку вовсе.
    """
    diverged = {
        word: (Grade.from_experience(exp), Grade.from_title(word))
        for word, exp in _GRADE_TO_EXP
        if Grade.from_title(word) is not None
        and Grade.from_title(word) is not Grade.from_experience(exp)
    }
    assert diverged == {
        "middle": (Grade.JUNIOR, Grade.MIDDLE),
        "mid":    (Grade.JUNIOR, Grade.MIDDLE),
        "senior": (Grade.MIDDLE, Grade.SENIOR),
    }
