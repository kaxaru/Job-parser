"""Адаптер devitjobs.uk — британская IT-доска, вся выдача одним запросом.

Здесь проверяются маппинг внешней схемы в домен (ACL) и ретраи транспорта — сеть подменена,
живых запросов нет.

Три инварианта, ради которых тесты и написаны:
  * `hasVisaSponsorship` приходит СТРОКОЙ «Yes»/«No» и непуст у всех записей, поэтому
    булева проверка поля дала бы «спонсируют» на каждой карточке — включая 2056 явных «No»
    (свойство реальной выдачи 13.08.2026);
  * описания у эндпоинта нет вовсе, и детект стека держится на структурных полях;
  * пустой ответ транспорта ретраится с длинным бэкоффом. Источник — один запрос, и без
    ретраев его сбой обнулял devitjobs, а санити-гейт из-за этого отбраковывал ВЕСЬ срез
    сбора (06–10.09.2026: четыре прогона из пяти).
"""
import asyncio
import itertools
import json

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net import http as H
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


# ── ретраи транспорта ─────────────────────────────────────────────────────────
# Регрессия 06-10.09.2026: единственный запрос без ретраев падал на пике веерного этапа 1,
# devitjobs давал 0 вакансий, и санити-гейт отбраковывал весь срез сбора — четыре дня из пяти.

_ONE_CARD = json.dumps([_dev()]).encode()


class _Log:
    """Перехват строк лога: у loguru формат — str.format с позиционными аргументами."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg.format(*args))

    def info(self, msg, *args):
        pass

    def debug(self, msg, *args):
        pass


def _transport(monkeypatch, bodies, *, attempt_seconds=0.0):
    """Подменить сеть заготовленными ответами (последний залипает), паузы и часы попыток.

    Подменяется `net/http.py` — там теперь живёт сам ретрай-цикл (`fetch_bytes_retry`), —
    а `log` остаётся у адаптера: сообщения и хук пустой попытки пишет он. Часы подменяются
    у модуля сетевого примитива, а не у `time`: глобальный `time.monotonic` читает event loop,
    и его подмена сломала бы сам `asyncio.run`."""
    calls: list[dict] = []
    sleeps: list[float] = []
    log = _Log()

    async def fake_fetch(url, **kw):
        calls.append({"url": url, **kw})
        return bodies[min(len(calls) - 1, len(bodies) - 1)]

    async def fake_sleep(delay):
        sleeps.append(delay)

    ticks = itertools.cycle([0.0, attempt_seconds])
    monkeypatch.setattr(H, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(H.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(H, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(devitjobs, "log", log)
    return calls, sleeps, log


def test_devitjobs_empty_transport_is_retried_and_data_of_a_later_attempt_is_taken(monkeypatch):
    calls, sleeps, _ = _transport(monkeypatch, [None, None, _ONE_CARD])
    got = asyncio.run(devitjobs.DevitjobsSource().collect())
    assert [r.id for r in got] == ["devitjobs_6a1f84e8b61b48b22e9b4883"]
    assert len(calls) == 3
    assert sleeps == [30.0, 60.0]


def test_devitjobs_all_four_attempts_empty_gives_no_vacancies(monkeypatch):
    calls, sleeps, _ = _transport(monkeypatch, [None])
    assert asyncio.run(devitjobs.DevitjobsSource().collect()) == []
    assert len(calls) == 4
    assert sleeps == [30.0, 60.0, 120.0]          # после последней попытки не ждём


def test_devitjobs_request_has_its_own_timeout_instead_of_the_default_25s(monkeypatch):
    calls, _, _ = _transport(monkeypatch, [_ONE_CARD])
    asyncio.run(devitjobs.DevitjobsSource().collect())
    assert calls[0]["max_time"] == 90


def test_devitjobs_empty_attempt_logs_its_duration_against_the_limit(monkeypatch):
    _, _, log = _transport(monkeypatch, [None], attempt_seconds=90.0)
    asyncio.run(devitjobs.DevitjobsSource().collect())
    assert log.warnings == [
        "devitjobs: попытка 1/4 пустая за 90с (лимит 90с)",
        "devitjobs: попытка 2/4 пустая за 90с (лимит 90с)",
        "devitjobs: попытка 3/4 пустая за 90с (лимит 90с)",
        "devitjobs: попытка 4/4 пустая за 90с (лимит 90с)",
        "devitjobs: выдача не получена (попыток: 4) — источник пропущен",
    ]


def test_devitjobs_answer_that_is_not_json_is_not_retried(monkeypatch):
    calls, sleeps, _ = _transport(monkeypatch, [b"<html>maintenance</html>"])
    assert asyncio.run(devitjobs.DevitjobsSource().collect()) == []
    assert len(calls) == 1
    assert sleeps == []
