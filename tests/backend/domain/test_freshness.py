"""Тесты тайминга вакансии: возраст, разрыв переоткрытия, классификация свежести."""
import datetime

from hrwork.domain import freshness
from hrwork.domain.freshness import FreshnessClass

# фиксированное «сегодня» -> детерминизм (2026-07-05 UTC)
TODAY = datetime.datetime(2026, 7, 5, tzinfo=datetime.timezone.utc)


def _iso(days_ago: int) -> str:
    return (TODAY - datetime.timedelta(days=days_ago)).isoformat()


def test_parse_dt_iso_and_unix():
    assert freshness.parse_dt("2026-04-30T08:20:03+03:00") is not None
    assert freshness.parse_dt(1782188439) is not None
    assert freshness.parse_dt("1782188439") is not None
    assert freshness.parse_dt(None) is None
    assert freshness.parse_dt("") is None
    assert freshness.parse_dt("не дата") is None


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


def test_parse_dt_handles_hirify_z_suffix():
    # hirify отдаёт '...Z' с микросекундами — Python 3.10 fromisoformat не ест 'Z'
    from hrwork.domain.freshness import parse_dt
    dt = parse_dt("2026-07-06T07:26:02.000000Z")
    assert dt is not None and dt.tzinfo is not None
