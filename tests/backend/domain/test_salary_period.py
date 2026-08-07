"""SalaryPeriod и Salary.monthly — период вилки как доменное понятие.

ВСЯ вилка в домене месячная: по ней считаются медианы, срезы аналитики и сортировка ленты.
Пересчёт живёт здесь, а не в адаптерах — до 07.08.2026 его копировали hirify, himalayas и
web3.career, и порог «это годовая?» разошёлся ($25 000 против 15 000). Один и тот же вопрос
обязан иметь один ответ, поэтому эти тесты — про домен, а не про источник.
"""
import pytest

from hrwork.domain.salary import Salary, SalaryPeriod


@pytest.mark.parametrize("code, expected", [
    ("hour", SalaryPeriod.HOUR),
    ("hourly", SalaryPeriod.HOUR),
    ("HOUR", SalaryPeriod.HOUR),                 # web3.career пишет капсом
    ("month", SalaryPeriod.MONTH),
    ("monthly", SalaryPeriod.MONTH),             # himalayas
    ("year", SalaryPeriod.YEAR),
    ("annual", SalaryPeriod.YEAR),               # himalayas
    ("  Annual  ", SalaryPeriod.YEAR),
    ("weekly", None),                            # незнакомое -> None, не выдумываем
    ("", None),
    (None, None),
])
def test_from_code_parses_portal_dialects(code, expected):
    assert SalaryPeriod.from_code(code) is expected


@pytest.mark.parametrize("usd_mid, expected", [
    (12, SalaryPeriod.HOUR),                     # < $300 -> почасовая
    (299, SalaryPeriod.HOUR),
    (3000, SalaryPeriod.MONTH),                  # между порогами -> месячная
    (25_000, SalaryPeriod.MONTH),                # ровно порог — ещё месячная
    (70_000, SalaryPeriod.YEAR),                 # > $25k -> годовая
    (None, SalaryPeriod.MONTH),                  # величины нет -> месяц по умолчанию
    (0, SalaryPeriod.MONTH),
])
def test_infer_by_magnitude(usd_mid, expected):
    assert SalaryPeriod.infer(usd_mid) is expected


@pytest.mark.parametrize("period, amount, expected", [
    (SalaryPeriod.HOUR, 12, 1920),               # ×160 раб.часов в месяце
    (SalaryPeriod.YEAR, 72_000, 6000),           # /12
    (SalaryPeriod.MONTH, 3000, 3000),
    (SalaryPeriod.YEAR, 50_000, 4167),           # округление, не усечение
    (SalaryPeriod.YEAR, None, None),
    (SalaryPeriod.MONTH, "мусор", None),
])
def test_to_monthly(period, amount, expected):
    assert period.to_monthly(amount) == expected


# ── Salary.monthly ─────────────────────────────────────────────────────────────

def test_monthly_builds_the_range():
    s = Salary.monthly(120_000, 180_000, "USD", SalaryPeriod.YEAR)
    assert (s.frm, s.to, s.currency, s.gross) == (10_000, 15_000, "USD", False)


def test_monthly_without_period_drops_the_range():
    """Домен НЕ угадывает период сам: можно ли угадывать — знание о качестве данных
    конкретного портала, и решает адаптер. У himalayas поле есть почти везде, и пустое
    там аномалия; кому инференс нужен — передаёт SalaryPeriod.infer(...) явно."""
    assert Salary.monthly(120_000, 180_000, "USD", None) is None


def test_monthly_empty_range_is_none():
    assert Salary.monthly(None, None, "USD", SalaryPeriod.YEAR) is None


def test_monthly_keeps_one_sided_range():
    s = Salary.monthly(None, 120_000, "EUR", SalaryPeriod.YEAR)
    assert (s.frm, s.to) == (None, 10_000)


def test_monthly_never_invents_currency():
    # на крипто-рынке платят и в стейблкоинах: подстановка USD наврала бы to_rub
    assert Salary.monthly(9000, 12_000, None, SalaryPeriod.MONTH).currency is None


def test_monthly_gross_flag_is_passed_through():
    assert Salary.monthly(100, 200, "RUR", SalaryPeriod.MONTH, gross=True).gross is True
