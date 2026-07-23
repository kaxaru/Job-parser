"""Тест санити-гейта сбора: деградированный срез источника не затирает полный кеш."""
from collections import Counter

import hh
from hrwork.config import COLLECT_MIN_RATIO, COLLECT_SANITY_MIN


def test_degraded_detects_source_collapse():
    prior = Counter({"hh": 30000, "hirify": 18000})
    # hh рухнул вдвое (блок), hirify цел -> флагим hh
    now = Counter({"hh": 12000, "hirify": 18000})
    assert hh._degraded_source(now, prior) == "hh"


def test_degraded_ok_when_stable_or_grows():
    prior = Counter({"hh": 30000, "hirify": 18000})
    now = Counter({"hh": 29500, "hirify": 19000})       # норм колебание/рост
    assert hh._degraded_source(now, prior) is None


def test_degraded_ignores_small_sources():
    # маленький источник ниже порога значимости — не флагим (шум)
    prior = Counter({"tiny": COLLECT_SANITY_MIN - 1})
    now = Counter({"tiny": 0})
    assert hh._degraded_source(now, prior) is None


def test_degraded_threshold_boundary():
    prior = Counter({"hh": 1000})
    just_below = Counter({"hh": int(1000 * COLLECT_MIN_RATIO) - 1})
    just_above = Counter({"hh": int(1000 * COLLECT_MIN_RATIO) + 1})
    assert hh._degraded_source(just_below, prior) == "hh"
    assert hh._degraded_source(just_above, prior) is None
