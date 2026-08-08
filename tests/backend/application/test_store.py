"""Тесты фасада ApplicationStore — делегирование в marks/quota/followup (без сети/браузера)."""
import json

import pytest

from hrwork.application.apply.outcome import ApplyChannel
from hrwork.application.apply.runtime import quota
from hrwork.application.apply.runtime.store import ApplicationStore
from hrwork.infrastructure.storage import MARK_VALUES, followup, marks

_QUOTA_DAY = "2026-08-08"      # «сегодня» квоты, фиксированное -> прогон не зависит от часов


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Изолируем все файлы состояния отклика во временный каталог.

    Сутки квоты заморожены: счётчик спрашивает `date.today()` на КАЖДОМ обращении, и
    `bump_quota(3)` в 23:59:59 с последующей проверкой уже за полночь давал 0 вместо 3
    (аудит 08.08.2026). Публичного шва у модуля нет — замораживаем `quota._today`."""
    monkeypatch.setattr(marks, "MARKS_FILE", tmp_path / "marks.json")
    monkeypatch.setattr(quota, "QUOTA_FILE", tmp_path / "apply_quota.json")
    monkeypatch.setattr(quota, "DATA_DIR", tmp_path)
    monkeypatch.setattr(quota, "_today", lambda: _QUOTA_DAY)
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


def test_mark_applied_writes_a_value_the_marks_filter_accepts(tmp_state):
    # СТРАЖ СОГЛАСОВАННОСТИ. `save_marks`/`load_marks` молча выбрасывают значение не из
    # MARK_VALUES, поэтому литерал мимо VO (опечатка "aplied") не упал бы — он бы просто
    # ПОТЕРЯЛ отметку, и крон откликнулся бы на ту же вакансию повторно.
    ApplicationStore().mark_applied("42")
    on_disk = json.loads((tmp_state / "marks.json").read_text(encoding="utf-8"))
    assert on_disk == {"42": "applied"}          # wire-формат ленты — литералом
    assert on_disk["42"] in MARK_VALUES          # и он переживает фильтр load_marks


def test_quota_delegates(tmp_state):
    s = ApplicationStore()
    assert s.applied_today() == 0
    assert s.bump_quota(3) == 3
    assert s.applied_today() == 3


def test_daily_cap_delegates_to_the_quota_module(tmp_state, monkeypatch):
    # Точное значение, а не `isinstance(..., int)`: обобщённая проверка проходила и на
    # заглушке, вернувшей 0 (это «отклики на сегодня запрещены»), и на любом чужом числе.
    # Литерал вместо `quota.DAILY_CAP_DEFAULT`: настоящий потолок берётся из HH_DAILY_APPLY_CAP,
    # то есть у запускающего с .env он законно другой — фасад обязан отдавать ЧТО ДАЛИ.
    monkeypatch.setattr(quota, "DAILY_CAP_DEFAULT", 137)
    assert ApplicationStore().daily_cap() == 137


def test_reconcile_quota_delegates(tmp_state):
    s = ApplicationStore()
    s.bump_quota(35)
    assert s.reconcile_quota(103) == 103         # факт из журнала выше счётчика -> поднимаем
    assert s.applied_today() == 103


def test_journal_and_applied_ids(tmp_state):
    s = ApplicationStore()
    s.log_applied("11", "Dev A", "u1", via=ApplyChannel.CRON)
    s.log_applied("22", "Dev B", "u2", via=ApplyChannel.FEED)
    log = {e["id"]: e for e in s.applied_log()}
    assert set(log) == {"11", "22"}
    assert log["11"]["via"] == "cron"                 # VO -> строка на диске
    assert log["22"]["via"] == "feed"
    assert s.applied_ids() == {"11", "22"}


def test_statuses_and_forms(tmp_state):
    s = ApplicationStore()
    s.save_statuses({"1": "invited"})
    assert s.statuses() == {"1": "invited"}
    assert s.add_form("5", "Опросник", "u5") is True      # новая анкета -> в счётчик прогона
    assert s.add_form("5", "Опросник", "u5") is False     # повтор -> счётчик очереди не растёт
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


def test_requeue_pending_delegates(tmp_state):
    s = ApplicationStore()
    s.enqueue("1", "u1", "A", "cover A")
    rec = s.pop_pending()
    assert s.requeue_pending(rec) == 1              # снятая, но не обработанная — вернулась
    assert [x["id"] for x in s.pending()] == ["1"]


def test_store_shares_disk_state_across_instances(tmp_state):
    # экземпляр без своего состояния: два ApplicationStore видят один диск
    ApplicationStore().mark_applied("7")
    assert ApplicationStore().marks() == {"7": "applied"}
    # записанный журнал читается корректно и как валидный JSONL
    ApplicationStore().log_applied("7", "N", "u", via=ApplyChannel.CRON)
    lines = (tmp_state / "applied_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["id"] == "7"
