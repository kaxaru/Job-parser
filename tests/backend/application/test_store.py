"""Тесты фасада ApplicationStore — делегирование в marks/quota/followup (без сети/браузера)."""
import json

import pytest

from hrwork.application.apply.outcome import ApplyChannel
from hrwork.application.apply.runtime import quota
from hrwork.application.apply.runtime.store import ApplicationStore
from hrwork.infrastructure.storage import followup, marks


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Изолируем все файлы состояния отклика во временный каталог."""
    monkeypatch.setattr(marks, "MARKS_FILE", tmp_path / "marks.json")
    monkeypatch.setattr(quota, "QUOTA_FILE", tmp_path / "apply_quota.json")
    monkeypatch.setattr(quota, "DATA_DIR", tmp_path)
    monkeypatch.setattr(followup, "APPLIED_LOG_FILE", tmp_path / "applied_log.jsonl")
    monkeypatch.setattr(followup, "RESPONSE_STATUS_FILE", tmp_path / "response_status.json")
    monkeypatch.setattr(followup, "FORM_VACANCIES_FILE", tmp_path / "form_vacancies.json")
    monkeypatch.setattr(followup, "PENDING_FILE", tmp_path / "apply_pending.json")
    monkeypatch.setattr(followup, "DATA_DIR", tmp_path)
    return tmp_path


def test_marks_merge_and_set(tmp_state):
    s = ApplicationStore()
    s.mark_applied("1")
    assert s.marks() == {"1": "applied"}
    s.merge_marks({"2": "rejected"})                # домержили, не потеряв "1"
    assert s.marks() == {"1": "applied", "2": "rejected"}
    s.set_marks({"9": "applied"})                   # полная замена (лента шлёт свой набор)
    assert s.marks() == {"9": "applied"}


def test_quota_delegates(tmp_state):
    s = ApplicationStore()
    assert s.applied_today() == 0
    assert s.bump_quota(3) == 3
    assert s.applied_today() == 3
    assert isinstance(s.daily_cap(), int)


def test_journal_and_applied_ids(tmp_state):
    s = ApplicationStore()
    s.log_applied("11", "Dev A", "u1", via=ApplyChannel.CRON)
    s.log_applied("22", "Dev B", "u2", via=ApplyChannel.FEED)
    log = {e["id"]: e for e in s.applied_log()}
    assert set(log) == {"11", "22"}
    assert log["11"]["via"] == "cron" and log["22"]["via"] == "feed"   # VO -> строка на диске
    assert s.applied_ids() == {"11", "22"}


def test_statuses_and_forms(tmp_state):
    s = ApplicationStore()
    s.save_statuses({"1": "invited"})
    assert s.statuses() == {"1": "invited"}
    s.add_form("5", "Опросник", "u5")
    assert "5" in s.forms()


def test_pending_queue_fifo(tmp_state):
    s = ApplicationStore()
    assert s.pop_pending() is None                  # пусто
    s.enqueue("1", "u1", "A", "cover A")
    s.enqueue("2", "u2", "B", "cover B")
    s.enqueue("1", "u1", "A", "cover A")            # дубль по id -> не добавляется
    assert len(s.pending()) == 2
    assert s.pop_pending()["id"] == "1"             # FIFO
    assert [x["id"] for x in s.pending()] == ["2"]


def test_store_shares_disk_state_across_instances(tmp_state):
    # экземпляр без своего состояния: два ApplicationStore видят один диск
    ApplicationStore().mark_applied("7")
    assert ApplicationStore().marks() == {"7": "applied"}
    # записанный журнал читается корректно и как валидный JSONL
    ApplicationStore().log_applied("7", "N", "u", via=ApplyChannel.CRON)
    lines = (tmp_state / "applied_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["id"] == "7"
