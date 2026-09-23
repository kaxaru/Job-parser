"""Сводка CRM по аккаунтам (RFC-004, R15): лента показывает, кто куда откликался.

Журналы обоих аккаунтов объединяются с полем `account` (легаси-строки -> владелец файла),
статусы и переписка тоже; при конфликте одной вакансии побеждает основной, но сам факт
двойного отклика виден в журнале.
"""
import datetime
import json
import os

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


_PULSE_EMPTY = {"today": 0, "window24": 0, "window_cap": 45, "window_free_at": None,
                "last_applied": None, "last_sync": None}


def test_accounts_lists_label_session_and_count(data_root, monkeypatch):
    monkeypatch.setattr(crm, "HH_APPLY_ROLLING_CAP", 45)
    _journal(data_root, [{"id": "1"}, {"id": "2"}])
    _session(data_root, "ok")
    _journal(data_root / "accounts" / "acc2", [{"id": "9"}])
    _session(data_root / "accounts" / "acc2", "expired")
    assert crm.accounts() == [
        {"code": "main", "label": "основной", "session": "ok", "applied": 2,
         "session_checked": "2026-09-15T21:00:00", **_PULSE_EMPTY},
        {"code": "acc2", "label": "Резюме B", "session": "expired", "applied": 1,
         "session_checked": "2026-09-15T21:00:00", **_PULSE_EMPTY},
    ]


# ── Пульс аккаунта (панель профилей, 24.09.2026): «отклики были? синк был? почему мало?» ──
# Владелец неделю задавал эти вопросы в чат, потому что лента на них не отвечала.
# Момент — полдень UTC, отклики расставлены так, что «сегодня/вчера» совпадают в любом поясе
# от -10 до +10: тест не зависит от часового пояса машины, на которой гоняется.
_PULSE_MOMENT = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)


def test_accounts_pulse_answers_today_window_last_apply_and_last_sync(data_root, monkeypatch):
    monkeypatch.setattr(crm, "HH_APPLY_ROLLING_CAP", 3)
    _journal(data_root, [
        {"id": "1", "ts": "2026-09-24T11:00:00+00:00"},
        {"id": "2", "ts": "2026-09-24T10:00:00+00:00"},
        {"id": "3", "ts": "2026-09-23T13:00:00+00:00"},    # вчера, но в скользящем окне (23ч назад)
        {"id": "4", "ts": "2026-09-20T09:00:00+00:00"},    # вне окна
    ])
    _status(data_root, {"1": "RESPONSE"})
    synced = datetime.datetime(2026, 9, 24, 7, 33, 1, tzinfo=datetime.timezone.utc).timestamp()
    os.utime(data_root / "response_status.json", (synced, synced))
    _session(data_root, "ok")
    _journal(data_root / "accounts" / "acc2", [{"id": "9", "ts": "2026-09-10T08:00:00+00:00"}])
    main, acc2 = crm.accounts(moment=_PULSE_MOMENT)
    assert main == {
        "code": "main", "label": "основной", "session": "ok", "applied": 4,
        "session_checked": "2026-09-15T21:00:00",
        "today": 2, "window24": 3, "window_cap": 3,
        "window_free_at": "2026-09-24T13:00:00+00:00",       # старейший в окне + 24ч
        "last_applied": "2026-09-24T11:00:00+00:00",
        "last_sync": "2026-09-24T07:33:01+00:00",
    }
    assert acc2 == {
        "code": "acc2", "label": "Резюме B", "session": "unknown", "applied": 1,
        "session_checked": None,
        "today": 0, "window24": 0, "window_cap": 3, "window_free_at": None,
        "last_applied": "2026-09-10T08:00:00+00:00", "last_sync": None,
    }


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
