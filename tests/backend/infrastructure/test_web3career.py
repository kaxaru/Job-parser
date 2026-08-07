"""Адаптер web3.career: маппинг внешней схемы в домен (ACL). Сеть не трогается.

Главное здесь — ЭВРИСТИКА зарплаты, единственная в проекте. `salary_unit` у портала заполнен
примерно у 5 % записей (замер 07.08.2026: HOUR у 2 и YEAR у 3 из 100), при том что сами
значения есть. Отбросить вилку без единицы, как сделано в himalayas, значило бы потерять 95 %
вилок, поэтому величина от ANNUAL_GUESS_MIN считается годовой.

Второй инвариант: валюта НЕ выдумывается. Её нет у ~94 % записей, а на крипто-рынке платят
и в USD, и в стейблкоинах — подстановка USD по умолчанию наврала бы рублёвой аналитике.
"""
import pytest

from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.sources import web3career


def _job(**kw):
    base = {
        "id": 152307,
        "title": "Senior Python Data Engineer",
        "company": "Crypto.com",
        "city": "singapore",
        "country": "singapore",
        "location": " Singapore",
        "is_remote": True,
        "tags": ["python", "backend", "data", "remote"],
        "description": "<p>Build data pipelines with Python and Airflow.</p>",
        "apply_url": "https://web3.career/r/3AzMyUTM__L86hzN",
        "date_epoch": 1784129943,
        "salary_min_value": None,
        "salary_max_value": None,
        "salary_currency": None,
        "salary_unit": None,
    }
    return {**base, **kw}


def test_maps_card_to_domain():
    r = web3career._normalize(_job())
    v = r.vacancy
    assert v.id == "web3_152307"
    assert v.name == "Senior Python Data Engineer"
    assert v.employer == "Crypto.com"
    assert v.source == "web3"
    assert v.schedule is Schedule.REMOTE
    assert r.url == "https://web3.career/r/3AzMyUTM__L86hzN"


@pytest.mark.parametrize("is_remote, expected", [
    (True, Schedule.REMOTE),
    (False, Schedule.OFFICE),
])
def test_remote_flag_becomes_schedule(is_remote, expected):
    assert web3career._normalize(_job(is_remote=is_remote)).vacancy.schedule is expected


def test_unix_timestamp_becomes_iso():
    # 1784129943 -> 2026-07-15T15:39:03Z (сверено отдельным пересчётом, не выводом кода)
    assert web3career._normalize(_job()).vacancy.created_at == "2026-07-15T15:39:03+00:00"


def test_grade_is_not_invented_from_tags():
    # в tags бывают junior/lead, но это свободные метки вперемешку со стеком, а не шкала
    v = web3career._normalize(_job(tags=["junior", "entry-level", "python"])).vacancy
    assert v.experience is None


# ── зарплата ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("unit, minimum, maximum, expected_from, expected_to", [
    ("YEAR", 120_000, 180_000, 10_000, 15_000),      # /12
    ("MONTH", 9_000, 12_000, 9_000, 12_000),         # как есть
    ("HOUR", 80, 120, 12_800, 19_200),               # ×160 ч/мес
])
def test_known_unit_is_used(unit, minimum, maximum, expected_from, expected_to):
    s = web3career._normalize(_job(salary_unit=unit, salary_min_value=minimum,
                                   salary_max_value=maximum,
                                   salary_currency="USD")).vacancy.salary
    assert (s.frm, s.to) == (expected_from, expected_to)


# Инференс периода запрашивается адаптером ЯВНО и считается доменом (SalaryPeriod.infer,
# порог $25 000 — общий для hirify/web3). Раньше здесь был свой порог 15 000: один и тот
# же вопрос имел два разных ответа в зависимости от портала.
@pytest.mark.parametrize("minimum, maximum, expected_from, expected_to", [
    (150_000, 180_000, 12_500, 15_000),   # > порога -> годовая, делим на 12
    (40_000, 50_000, 3_333, 4_167),       # > порога -> годовая (округление, не усечение)
    (9_000, 12_000, 9_000, 12_000),       # < порога -> уже месячная, не трогаем
    (None, 24_999, None, 24_999),         # ровно под порогом -> месячная, вилка «до»
])
def test_missing_unit_is_guessed_by_magnitude(minimum, maximum, expected_from, expected_to):
    s = web3career._normalize(_job(salary_unit=None, salary_min_value=minimum,
                                   salary_max_value=maximum)).vacancy.salary
    assert (s.frm, s.to) == (expected_from, expected_to)


def test_guess_uses_the_shared_domain_threshold():
    # порог один на все порталы — живёт в SalaryPeriod.infer, не в адаптере
    from hrwork.domain.salary import SalaryPeriod
    assert SalaryPeriod.infer(30_000) is SalaryPeriod.YEAR
    s = web3career._normalize(_job(salary_min_value=30_000,
                                   salary_max_value=None)).vacancy.salary
    assert s.frm == 2500


def test_currency_is_never_invented():
    # без валюты вилка сохраняется, но в рублёвые срезы не попадёт — подставлять USD нельзя
    s = web3career._normalize(_job(salary_min_value=150_000,
                                   salary_max_value=180_000)).vacancy.salary
    assert s.currency is None
    assert s.frm == 12_500


def test_currency_is_kept_when_given():
    s = web3career._normalize(_job(salary_min_value=120_000, salary_max_value=None,
                                   salary_unit="YEAR",
                                   salary_currency="EUR")).vacancy.salary
    assert s.currency == "EUR"


def test_missing_range_is_none():
    assert web3career._normalize(_job()).vacancy.salary is None


def test_foreign_salary_is_not_taxed_as_russian():
    s = web3career._normalize(_job(salary_min_value=150_000)).vacancy.salary
    assert s.gross is False


# ── город ──────────────────────────────────────────────────────────────────────

def test_city_prefers_city_field_and_restores_case():
    assert web3career._normalize(_job()).vacancy.city == "Singapore"


def test_city_falls_back_to_country():
    v = web3career._normalize(_job(city="", location="", country="germany")).vacancy
    assert v.city == "Germany"


def test_fully_remote_without_place_is_labelled():
    v = web3career._normalize(_job(city="", location="", country="")).vacancy
    assert v.city == "Remote"
