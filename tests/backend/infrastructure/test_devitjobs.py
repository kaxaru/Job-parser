"""Адаптер devitjobs.uk — британская IT-доска, вся выдача одним запросом.

Здесь проверяется только маппинг внешней схемы в домен (ACL) — сеть не трогается.

Два инварианта, ради которых тесты и написаны (оба — свойства реальной выдачи 13.08.2026):
  * `hasVisaSponsorship` приходит СТРОКОЙ «Yes»/«No» и непуст у всех записей, поэтому
    булева проверка поля дала бы «спонсируют» на каждой карточке — включая 2056 явных «No»;
  * описания у эндпоинта нет вовсе, и детект стека держится на структурных полях.
"""
import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.sources import devitjobs

pytestmark = pytest.mark.usefixtures("apply_defaults")


def _dev(**kw):
    base = {
        "_id": "6a1f84e8b61b48b22e9b4883",
        "name": "AI Deployment Engineer",
        "company": "Emponics",
        "actualCity": "Almondsbury",
        "annualSalaryFrom": 80000,
        "annualSalaryTo": 130000,
        "expLevel": "Senior",
        "jobType": "Full-Time",
        "workplace": "office",
        "hasVisaSponsorship": "No",
        "companySize": "<50",
        "companyType": "Services",
        "technologies": ["Python", "Docker", "Kubernetes"],
        "filterTags": ["Cloud"],
        "techCategory": "Data",
        "activeFrom": "2026-08-05T14:27:12.425+00:00",
        "jobUrl": "Emponics-AI-Deployment-Engineer",
        "isPaused": False,
    }
    return {**base, **kw}


def test_devitjobs_maps_card_to_domain():
    r = devitjobs._normalize(_dev())
    v = r.vacancy
    assert v.id == "devitjobs_6a1f84e8b61b48b22e9b4883"
    assert v.name == "AI Deployment Engineer"
    assert v.employer == "Emponics"
    assert v.city == "Almondsbury"
    assert v.source == "devitjobs"
    assert r.url == "https://devitjobs.uk/jobs/Emponics-AI-Deployment-Engineer"


def test_devitjobs_annual_pounds_become_monthly():
    """Вилка годовая и в фунтах (валюты в схеме нет — см. devitjobs.CURRENCY).
    Считано отдельно: 80000/12 = 6666.7, 130000/12 = 10833.3."""
    s = devitjobs._normalize(_dev()).vacancy.salary
    assert (s.frm, s.to, s.currency) == (6667, 10833, "GBP")


def test_devitjobs_missing_range_is_none():
    assert devitjobs._normalize(
        _dev(annualSalaryFrom=None, annualSalaryTo=None)).vacancy.salary is None


def test_devitjobs_foreign_salary_is_not_taxed_as_russian():
    assert devitjobs._normalize(_dev()).vacancy.salary.gross is False


@pytest.mark.parametrize("flag, expected_prefix", [
    ("Yes", "Виза: спонсируют"),
    ("No", "Виза: не спонсируют"),
    ("", "Виза: не спонсируют"),
])
def test_devitjobs_visa_flag_is_read_as_a_word_not_as_truthiness(flag, expected_prefix):
    """Поле — строка, и непустая у всех 2058 записей. `bool(поле)` дал бы «спонсируют»
    даже на «No»; правильный ответ есть только у двух карточек из выдачи."""
    r = devitjobs._normalize(_dev(hasVisaSponsorship=flag))
    assert r.requirement.startswith(expected_prefix)


@pytest.mark.parametrize("workplace, expected", [
    ("remote", Schedule.REMOTE),
    ("hybrid", Schedule.HYBRID),
    ("office", Schedule.OFFICE),
    ("", Schedule.OFFICE),                    # неизвестное -> доменный дефолт
])
def test_devitjobs_workplace_becomes_schedule(workplace, expected):
    assert devitjobs._normalize(_dev(workplace=workplace)).vacancy.schedule is expected


@pytest.mark.parametrize("level, job_type, expected", [
    ("Junior", "Full-Time", Experience.BETWEEN_1_3),
    ("Regular", "Full-Time", Experience.BETWEEN_1_3),   # британское имя середины
    ("Senior", "Full-Time", Experience.BETWEEN_3_6),
    ("Lead", "Full-Time", Experience.MORE_6),
    ("", "Internship", Experience.NONE),                # грейда нет — говорит тип занятости
    ("", "Full-Time", None),
])
def test_devitjobs_grade_labels_map_to_experience(level, job_type, expected):
    v = devitjobs._normalize(_dev(expLevel=level, jobType=job_type)).vacancy
    assert v.experience is expected


def test_devitjobs_stack_is_detected_without_any_description():
    """Эндпоинт `jobsLight` описания не отдаёт — стек обязан вычисляться из структурных
    полей, иначе источник пришёл бы в ленту вообще без техов."""
    r = devitjobs._normalize(_dev())
    assert r.vacancy.techs == ["Python", "Docker", "Kubernetes"]
    assert r.enriched is False
    assert r.description_html == ""


def test_devitjobs_paused_vacancy_is_dropped():
    # снятая с публикации — штатный отсев, а не сбойная карточка
    assert devitjobs._normalize(_dev(isPaused=True)) is None
