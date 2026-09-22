"""Код аккаунта HH (RFC-004, R3): разбор `HR_ACCOUNT` до любого обращения к диску.

Код становится сегментом пути `data/accounts/<code>/`, поэтому `..`, слеши и пробелы обязаны
отбиваться в чистом разборе, а не где-то после склейки пути.
"""
import pytest

from hrwork.domain.account import AccountError, HhAccount, parse_account_code


@pytest.mark.parametrize("raw, expected", [("", "main"), ("main", "main"), (" acc2 ", "acc2"),
                                           ("resume_b-2", "resume_b-2")])
def test_code_is_parsed(raw, expected):
    assert parse_account_code(raw) == expected


@pytest.mark.parametrize("raw", ["Acc2", "acc 2", "../main", "acc2/x", "-acc", "a" * 33])
def test_malformed_code_is_refused(raw):
    with pytest.raises(AccountError) as err:
        parse_account_code(raw)
    assert str(err.value) == (f"HR_ACCOUNT={raw!r}: код аккаунта — строчная латиница, цифры, "
                              f"«_» и «-», до 32 символов")


@pytest.mark.parametrize("code, expected", [("main", True), ("acc2", False)])
def test_only_main_is_main(code, expected):
    assert HhAccount(code=code, label="", data_dir=None).is_main is expected
