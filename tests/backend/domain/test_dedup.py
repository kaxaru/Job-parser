"""Кросс-портальный дедуп: ключ склейки и выбор победителя (чистая логика, без сети)."""
from dataclasses import dataclass

import pytest

from hrwork.domain.dedup import dedup_cross_source, dedup_key, norm_employer, norm_title
from hrwork.domain.parsing import build_vacancy

PRIORITY = ["hh", "hirify", "talanto", "getmatch"]


def vac(name="Python-разработчик", employer="Рога и Копыта", source="hh", vid="1", city="Москва"):
    return build_vacancy(vid=vid, name=name, city=city, city_id="", salary=None,
                         experience=None, schedule=None, detect_text=name, employer=employer,
                         created_at=None, published_at=None, responses=None, source=source)


@dataclass
class Rec:
    """Минимальный носитель .vacancy — дедуп не знает про VacancyRecord."""
    vacancy: object


def rec(**kw):
    return Rec(vacancy=vac(**kw))


# ── ключ ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("ООО «Рога и Копыта»", "рога и копыта"),
    ('ЗАО "Рога и Копыта"', "рога и копыта"),
    ("Рога и Копыта", "рога и копыта"),
    ("ИП Константинов Семен Павлович", "константинов семен павлович"),
])
def test_legal_form_does_not_split_employer(raw, expected):
    assert norm_employer(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("C#/.NET-разработчик (офис, Москва)", "c net разработчик"),
    ("C#/.NET-разработчик", "c net разработчик"),
    ("Программист 1С:ERP", "программист 1с erp"),
    ("Ёж-разработчик", "еж разработчик"),
])
def test_parenthetical_and_punctuation_do_not_split_title(raw, expected):
    assert norm_title(raw) == expected


def test_grade_stays_in_key():
    """«Junior Python» и «Senior Python» у одного работодателя — РАЗНЫЕ вакансии."""
    assert dedup_key(vac(name="Junior Python-разработчик")) != \
           dedup_key(vac(name="Senior Python-разработчик"))


@pytest.mark.parametrize("employer,name", [("", "Python-разработчик"), ("Рога", ""), ("", "")])
def test_anonymous_vacancy_is_not_deduped(employer, name):
    """Пустой работодатель или тайтл -> ключа нет: иначе склеились бы разные компании."""
    assert dedup_key(vac(employer=employer, name=name)) is None


# ── склейка ─────────────────────────────────────────────────────────────────────────────

def test_copy_from_lower_priority_portal_is_dropped():
    recs = [rec(source="hh", vid="1"), rec(source="talanto", vid="2")]
    kept, dropped = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1"]
    assert dropped == {"talanto": 1}


def test_winner_is_by_priority_not_by_input_order():
    """hh идёт первым в SOURCES, потому что с него работает автоотклик."""
    recs = [rec(source="talanto", vid="2"), rec(source="hh", vid="1")]
    kept, _ = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1"]


def test_duplicates_inside_one_portal_are_kept():
    """hh переопубликовывает объявления — это его история публикаций, не наше дело."""
    recs = [rec(source="hh", vid="1"), rec(source="hh", vid="2")]
    kept, dropped = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1", "2"]
    assert dropped == {}


def test_different_employers_are_not_merged():
    recs = [rec(source="hh", vid="1", employer="Рога"),
            rec(source="talanto", vid="2", employer="Копыта")]
    kept, _ = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1", "2"]


def test_unknown_source_loses_to_known():
    recs = [rec(source="hh", vid="1"), rec(source="новый_портал", vid="2")]
    kept, dropped = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1"]
    assert dropped == {"новый_портал": 1}


def test_order_of_survivors_is_preserved():
    recs = [rec(source="hh", vid="1", name="A"), rec(source="talanto", vid="2", name="B"),
            rec(source="hh", vid="3", name="C")]
    kept, _ = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1", "2", "3"]


# ── Регрессия 01.08.2026: город в ключе ломал склейку ────────────────────────────────────
# Порталы округляют локацию по-разному: hh пишет «Москва», talanto — точное поселение.
# Замер по кешу 87k: 894 группы «разных» городов, в основном одна и та же вакансия.

@pytest.mark.parametrize("hh_city,other_city", [
    ("Москва", "Химки"),
    ("Москва", "Зеленоград"),
    ("Москва", "рабочий посёлок Быково"),
])
def test_same_vacancy_merges_despite_city_granularity(hh_city, other_city):
    recs = [rec(source="hh", vid="1", name="Программист 1С", employer="Факел", city=hh_city),
            rec(source="talanto", vid="2", name="Программист 1С", employer="Факел",
                city=other_city)]
    kept, dropped = dedup_cross_source(recs, PRIORITY)
    assert [r.vacancy.id for r in kept] == ["1"]
    assert dropped == {"talanto": 1}
