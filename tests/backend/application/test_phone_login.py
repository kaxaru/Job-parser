"""Вход по телефону+SMS и диспетчеризация способа входа (RFC-004) — `browser.py`.

`_auto_login` — единственная точка, через которую run/apply/login добираются до формы входа;
способ выбирается по `ACCOUNT.login`. Инвариант R4 (не-основной НИКОГДА не вводит учётные
данные основного) обязан держаться при любом способе. Номера в тестах вымышленные.
"""
import pytest

from hrwork.application.apply import browser
from hrwork.domain.account import HhAccount

PHONE = HhAccount(code="acc2", label="B", data_dir=None, login="phone")
MANUAL = HhAccount(code="acc2", label="B", data_dir=None, login="manual")


class _RecordingPage:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)
        return lambda *a, **k: None


@pytest.mark.parametrize("raw, expected", [
    ("+7 999 123-45-67", "9991234567"),
    ("8 (999) 123-45-67", "9991234567"),
    ("79991234567", "9991234567"),
    ("9991234567", "9991234567"),
    ("8-800-555-35-35", "8005553535"),
    ("123", ""),                       # мало цифр
    ("790012345678", ""),              # 12 цифр — не РФ-номер
    ("", ""),
])
def test_national_number(raw, expected):
    assert browser._national_number(raw) == expected


def test_dispatch_phone_account_uses_phone_login(monkeypatch):
    monkeypatch.setattr(browser, "ACCOUNT", PHONE)
    seen: list[object] = []
    monkeypatch.setattr(browser, "_phone_login", lambda page: seen.append(page) or True)
    page = _RecordingPage()
    assert browser._auto_login(page) is True
    assert seen == [page]


def test_dispatch_manual_account_never_touches_the_page(monkeypatch):
    monkeypatch.setattr(browser, "ACCOUNT", MANUAL)
    monkeypatch.setattr(browser, "HH_EMAIL", "main@example.com")
    monkeypatch.setattr(browser, "HH_PASSWORD", "main-password")
    fills: list[tuple[str, str]] = []
    monkeypatch.setattr(browser, "_try_fill", lambda page, sel, value: fills.append((sel, value)) or True)
    page = _RecordingPage()
    assert browser._auto_login(page) is False
    assert fills == []
    assert page.calls == []


def test_phone_login_without_a_phone_number_stops_before_the_page(monkeypatch):
    """login=phone, но HR_LOGIN_PHONE пуст -> False до любого обращения к странице/форме."""
    monkeypatch.setattr(browser, "ACCOUNT", PHONE)
    monkeypatch.setattr(browser, "HR_LOGIN_PHONE", "")
    fills: list[tuple[str, str]] = []
    monkeypatch.setattr(browser, "_try_fill", lambda page, sel, value: fills.append((sel, value)) or True)
    page = _RecordingPage()
    assert browser._phone_login(page) is False
    assert fills == []
    assert page.calls == []
