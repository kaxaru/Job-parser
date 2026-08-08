"""Тесты тайминга вакансии: возраст, разрыв переоткрытия, классификация свежести."""
import datetime

import pytest

from hrwork.domain import freshness
from hrwork.domain.freshness import FreshnessClass

# фиксированное «сегодня» -> детерминизм (2026-07-05 UTC)
TODAY = datetime.datetime(2026, 7, 5, tzinfo=datetime.timezone.utc)
UTC = datetime.timezone.utc


def _iso(days_ago: int) -> str:
    return (TODAY - datetime.timedelta(days=days_ago)).isoformat()


# Сравнивается МОМЕНТ, а не «что-то вернулось» (аудит 08.08.2026, находка 61). `is not None`
# проходил и на парсере, который считает unix-секунды миллисекундами (1782188439 -> 1970-01-21)
# или трактует 'Z' как локальную зону (сдвиг на 3 часа) — а от этого момента считаются возраст,
# ghost-класс и отбор под отклик. Ожидаемое — литерал в UTC: 1782188439 = 2026-06-23 04:20:39Z.
@pytest.mark.parametrize("raw, expected", [
    ("2026-04-30T08:20:03+03:00", datetime.datetime(2026, 4, 30, 5, 20, 3, tzinfo=UTC)),
    (1782188439,                  datetime.datetime(2026, 6, 23, 4, 20, 39, tzinfo=UTC)),
    ("1782188439",                datetime.datetime(2026, 6, 23, 4, 20, 39, tzinfo=UTC)),
    # hirify отдаёт '...Z' с микросекундами — Python 3.10 fromisoformat не ест 'Z'
    ("2026-07-06T07:26:02.000000Z", datetime.datetime(2026, 7, 6, 7, 26, 2, tzinfo=UTC)),
    # без зоны — считаем UTC, а не локальным временем машины
    ("2026-07-06T07:26:02",         datetime.datetime(2026, 7, 6, 7, 26, 2, tzinfo=UTC)),
])
def test_parse_dt_returns_the_exact_moment(raw, expected):
    assert freshness.parse_dt(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "не дата"])
def test_parse_dt_returns_none_for_non_dates(raw):
    assert freshness.parse_dt(raw) is None


def test_age_days():
    assert freshness.age_days(_iso(10), today=TODAY) == 10
    assert freshness.age_days(None, today=TODAY) is None


def test_republish_gap():
    # создана 60 дн назад, опубликована 6 дн назад -> переоткрывали (разрыв 54)
    gap = freshness.republish_gap_days(_iso(60), _iso(6))
    assert gap == 54
    assert freshness.republish_gap_days(None, _iso(6)) is None


def test_classify_fresh_recent_ghost():
    assert freshness.classify(_iso(5), today=TODAY) is FreshnessClass.FRESH      # <=30
    assert freshness.classify(_iso(30), today=TODAY) is FreshnessClass.FRESH     # граница
    assert freshness.classify(_iso(45), today=TODAY) is FreshnessClass.RECENT    # 30..60
    assert freshness.classify(_iso(90), today=TODAY) is FreshnessClass.GHOST     # >60
    assert freshness.classify(None, today=TODAY) is FreshnessClass.UNKNOWN


def test_is_ghost():
    assert freshness.is_ghost(_iso(75), today=TODAY) is True
    assert freshness.is_ghost(_iso(10), today=TODAY) is False
