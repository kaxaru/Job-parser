"""Вход, личность и статус сессии второго аккаунта (RFC-004: R4, R5, R19).

Цена ошибки здесь — необратимые отклики не от того резюме: форма входа второго аккаунта,
заполненная паролем основного, или сессия, в которой оказался не тот пользователь HH.
id пользователей в тестах вымышленные.
"""
import json
import sys
import types

import pytest

from hrwork.application.apply import account_session, autoclick, browser, session
from hrwork.application.apply.runtime import lock, watchdog
from hrwork.domain.account import HhAccount

ACC2 = HhAccount(code="acc2", label="B", data_dir=None)   # data_dir сверке не нужен
PIN_12345678 = "ef797c8118f02dfb"     # sha256("12345678")[:16]
PIN_87654321 = "e24df920078c3dd4"     # sha256("87654321")[:16]


@pytest.fixture
def files(tmp_path, monkeypatch):
    """Состояние сессии, пин и статус текущего аккаунта + корень данных для чужих пинов."""
    monkeypatch.setattr(session, "STATE_FILE", tmp_path / "state" / "hh_state.json")
    monkeypatch.setattr(account_session, "IDENTITY_FILE", tmp_path / "state" / "account_identity.json")
    monkeypatch.setattr(account_session, "SESSION_STATUS_FILE", tmp_path / "state" / "session_status.json")
    monkeypatch.setattr(account_session, "DATA_ROOT", tmp_path / "data")
    (tmp_path / "state").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


def _session_of(files, user_id):
    (files / "state" / "hh_state.json").write_text(json.dumps({"cookies": [
        {"name": "_xsrf", "value": "x", "domain": ".hh.ru"},
        {"name": "_hi", "value": user_id, "domain": ".hh.ru"},
    ]}), encoding="utf-8")


def _pin(path, pin):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"identity": pin}), encoding="utf-8")


# ── R4: пароль основного не попадает в форму входа второго аккаунта ──
class _RecordingPage:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)
        return lambda *a, **k: None


def test_second_account_never_types_main_credentials(monkeypatch):
    monkeypatch.setattr(browser, "ACCOUNT", ACC2)
    monkeypatch.setattr(browser, "HH_EMAIL", "main@example.com")
    monkeypatch.setattr(browser, "HH_PASSWORD", "main-password")
    fills: list[tuple[str, str]] = []
    monkeypatch.setattr(browser, "_try_fill", lambda page, sel, value: fills.append((sel, value)) or True)
    page = _RecordingPage()
    assert browser._auto_login(page) is False
    assert fills == []
    assert page.calls == []


# ── R5: личность сессии ──
def test_first_login_pins_the_identity(files):
    _session_of(files, "12345678")
    assert account_session.identity_problem() is None
    assert json.loads((files / "state" / "account_identity.json").read_text("utf-8"))["identity"] == PIN_12345678


def test_same_user_passes(files):
    _session_of(files, "12345678")
    _pin(files / "state" / "account_identity.json", PIN_12345678)
    assert account_session.identity_problem() is None


def test_other_user_in_the_session_stops_the_run(files):
    _session_of(files, "87654321")
    _pin(files / "state" / "account_identity.json", PIN_12345678)
    assert account_session.identity_problem() == (
        "Аккаунт main: в сессии другой пользователь HH, не закреплённый в account_identity.json — "
        "прогон остановлен, войди заново: hh.py autoclick --login при HR_ACCOUNT=main")


def test_session_of_another_account_stops_before_pinning(files, monkeypatch):
    # вход не в тот аккаунт в окне --login: пина ещё нет, но личность уже закреплена за main
    monkeypatch.setattr(account_session, "ACCOUNT", ACC2)
    _session_of(files, "12345678")
    _pin(files / "data" / "account_identity.json", PIN_12345678)
    assert account_session.identity_problem() == (
        "Аккаунт acc2: в сессии пользователь HH аккаунта main — прогон остановлен, "
        "войди заново: hh.py autoclick --login при HR_ACCOUNT=acc2")
    assert not (files / "state" / "account_identity.json").exists()


def test_session_without_user_cookie_is_not_pinned(files):
    (files / "state" / "hh_state.json").write_text(json.dumps({"cookies": []}), encoding="utf-8")
    assert account_session.identity_problem() is None
    assert not (files / "state" / "account_identity.json").exists()


@pytest.mark.parametrize("user_id, expected_ok, expected_state", [
    ("12345678", True, "ok"),
    ("87654321", False, "foreign"),
])
def test_verify_session_records_the_state(files, user_id, expected_ok, expected_state):
    _session_of(files, user_id)
    _pin(files / "state" / "account_identity.json", PIN_12345678)
    assert account_session.verify_session() is expected_ok
    assert json.loads((files / "state" / "session_status.json").read_text("utf-8"))["state"] == expected_state


# ── R19: истёкшая сессия второго аккаунта — стоп и сигнал для баннера, без автовхода ──
def test_expired_second_account_session_is_recorded_and_stops(files, monkeypatch):
    import contextlib
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        types.SimpleNamespace(sync_playwright=contextlib.nullcontext))
    monkeypatch.setattr(browser, "ACCOUNT", ACC2)
    monkeypatch.setattr(autoclick, "ACCOUNT", ACC2)     # сообщение прогона собирает autoclick.run
    monkeypatch.setattr(watchdog, "_hang_watchdog", contextlib.nullcontext)
    monkeypatch.setattr(lock, "_single_instance", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(browser, "_launch", lambda *a, **k: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(browser, "_page", lambda ctx: object())
    monkeypatch.setattr(browser, "_session_state", lambda page: browser.LoginState.ANONYMOUS)
    applied: list[str] = []
    monkeypatch.setattr(autoclick, "_apply_batch", lambda *a, **k: applied.append("batch"))
    with pytest.raises(SystemExit) as stop:
        autoclick.run(autoclick.RunOptions(apply_limit=1))
    assert str(stop.value) == ("Аккаунт acc2: сессии нет, автовход не прошёл (капча/код/SMS) — "
                               "запусти: python hh.py autoclick --login при HR_ACCOUNT=acc2")
    assert json.loads((files / "state" / "session_status.json").read_text("utf-8"))["state"] == "expired"
    assert applied == []
