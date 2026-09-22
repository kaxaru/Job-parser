"""Сводка CRM по аккаунтам (RFC-004, R15): лента показывает, кто куда откликался.

Журналы обоих аккаунтов объединяются с полем `account` (легаси-строки -> владелец файла),
статусы и переписка тоже; при конфликте одной вакансии побеждает основной, но сам факт
двойного отклика виден в журнале.
"""
import json

import pytest

from hrwork.application.apply import account_session, crm, taken

MAIN, ACC2 = "data", "accounts/acc2"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(taken, "DATA_ROOT", tmp_path)
    (tmp_path / "accounts" / "acc2").mkdir(parents=True)
    (tmp_path / "accounts" / "acc2" / "account.json").write_text(
        json.dumps({"label": "Резюме B"}), encoding="utf-8")
    return tmp_path


def _journal(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "applied_log.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _status(folder, mapping):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "response_status.json").write_text(json.dumps(mapping), encoding="utf-8")


def _session(folder, state):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / account_session.SESSION_STATUS_FILE_NAME).write_text(
        json.dumps({"state": state, "ts": "2026-09-15T21:00:00"}), encoding="utf-8")


def test_accounts_lists_label_session_and_count(data_root):
    _journal(data_root, [{"id": "1"}, {"id": "2"}])
    _session(data_root, "ok")
    _journal(data_root / "accounts" / "acc2", [{"id": "9"}])
    _session(data_root / "accounts" / "acc2", "expired")
    assert crm.accounts() == [
        {"code": "main", "label": "основной", "session": "ok", "applied": 2},
        {"code": "acc2", "label": "Резюме B", "session": "expired", "applied": 1},
    ]


def test_account_without_session_file_reads_unknown(data_root):
    assert crm.accounts()[0]["session"] == "unknown"


def test_applied_tags_each_row_with_its_account(data_root):
    _journal(data_root, [{"id": "1", "name": "A"}])                       # легаси: без поля account
    _journal(data_root / "accounts" / "acc2", [{"id": "9", "name": "B", "account": "acc2"}])
    rows = {r["id"]: r["account"] for r in crm.applied()}
    assert rows == {"1": "main", "9": "acc2"}


def test_legacy_row_with_explicit_account_is_kept(data_root):
    # строка основного, дожурналенная синком с явным account — не перетираем владельцем файла
    _journal(data_root, [{"id": "1", "account": "main"}])
    assert crm.applied()[0]["account"] == "main"


def test_statuses_are_kept_per_account_not_merged(data_root):
    # вакансия 5 — отклик с ОБОИХ аккаунтов с РАЗНЫМИ статусами: храним оба, не «основной победил»
    _status(data_root, {"1": "DISCARD", "5": "DISCARD"})
    _status(data_root / "accounts" / "acc2", {"5": "RESPONSE", "9": "INTERVIEW"})
    assert crm.statuses() == {
        "1": {"main": "DISCARD"},
        "5": {"main": "DISCARD", "acc2": "RESPONSE"},
        "9": {"acc2": "INTERVIEW"},
    }
