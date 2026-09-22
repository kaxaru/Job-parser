"""Разрешение аккаунта HH по `HR_ACCOUNT` (RFC-004, R3) — `hrwork/accounts.py`.

Контракт строгий: опечатка в коде аккаунта обязана остановить процесс, а не завести молча
новый пустой аккаунт — с чистой квотой и без журнала он откликнулся бы на всё, что основной
уже разобрал. Папка аккаунта сама не создаётся ни при какой ошибке.
"""
import json

import pytest

from hrwork.accounts import resolve_account
from hrwork.domain.account import AccountError, HhAccount


def _account_dir(tmp_path, code="acc2", meta=None, profile=True):
    folder = tmp_path / "accounts" / code
    folder.mkdir(parents=True)
    if meta is not None:
        (folder / "account.json").write_text(json.dumps(meta), encoding="utf-8")
    if profile:
        (folder / "resume_profile.json").write_text("{}", encoding="utf-8")
    return folder


@pytest.mark.parametrize("raw", ["", "main", "  main "])
def test_empty_or_main_is_the_main_account_on_legacy_paths(tmp_path, raw):
    assert resolve_account(raw, tmp_path) == HhAccount(code="main", label="основной", data_dir=tmp_path)


def test_second_account_lives_in_its_own_folder(tmp_path):
    folder = _account_dir(tmp_path, meta={"label": "Резюме B", "login": "phone"})
    account = resolve_account("acc2", tmp_path)
    assert account == HhAccount(code="acc2", label="Резюме B", data_dir=folder, login="phone")
    assert account.is_main is False


def test_label_falls_back_to_the_code(tmp_path):
    _account_dir(tmp_path, meta={})
    assert resolve_account("acc2", tmp_path).label == "acc2"


# ── Способ входа (RFC-004): не-основной входит по phone или manual, но не паролем основного ──
def test_login_method_defaults_to_manual_when_absent(tmp_path):
    _account_dir(tmp_path, meta={"label": "B"})
    assert resolve_account("acc2", tmp_path).login == "manual"


def test_login_method_phone_is_read_from_meta(tmp_path):
    _account_dir(tmp_path, meta={"login": "phone"})
    assert resolve_account("acc2", tmp_path).login == "phone"


@pytest.mark.parametrize("bad", ["password", "sms", "email"])
def test_login_method_outside_the_allowed_set_stops(tmp_path, bad):
    _account_dir(tmp_path, meta={"login": bad})
    with pytest.raises(AccountError) as err:
        resolve_account("acc2", tmp_path)
    assert str(err.value) == (f"HR_ACCOUNT=acc2: login={bad!r} в account.json — "
                              "допустимо phone или manual (password только у основного)")


def test_main_account_stays_on_password_login(tmp_path):
    assert resolve_account("main", tmp_path).login == "password"


def test_unknown_account_stops_and_creates_nothing(tmp_path):
    with pytest.raises(AccountError) as err:
        resolve_account("acc3", tmp_path)
    assert str(err.value) == ("HR_ACCOUNT=acc3: нет data/accounts/acc3/account.json. "
                              "Аккаунт заводится вручную (RFC-004), папка сама не создаётся")
    assert not (tmp_path / "accounts").exists()


def test_account_without_its_own_profile_stops(tmp_path):
    _account_dir(tmp_path, meta={"label": "B"}, profile=False)
    with pytest.raises(AccountError) as err:
        resolve_account("acc2", tmp_path)
    assert str(err.value) == ("HR_ACCOUNT=acc2: нет data/accounts/acc2/resume_profile.json — "
                              "без него отбор шёл бы по дефолтам основного аккаунта")
