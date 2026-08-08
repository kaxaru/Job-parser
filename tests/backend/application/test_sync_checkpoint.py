"""Синк статусов из чатов: чекпойнты и дата отклика (без сети и без браузера).

АУДИТ 08.08.2026. `autoclick.py::sync_statuses` держал всё в памяти до конца прогона:
`save_statuses` / `save_chat_messages` стояли ПОСЛЕ цикла по ~1300 чатам (десятки минут,
watchdog на этот путь не распространяется — Chromium тут не поднимается). Kill на 800-м из
1300 терял ВСЮ скачанную переписку.

Отдельно: чат без сообщений отдаёт `chat.response_time -> ""`, а `followup.append_applied`
подставляет на пустой ts текущее время. Старый ручной отклик журналировался сегодняшней
датой НАВСЕГДА (журнал append-only), и воронка считала по ней латентность.
"""
from typing import Any

import pytest

from hrwork.application.apply import autoclick

REAL_RESPONSE_TS = "2026-08-01T10:00:00+03:00"


class _Req:
    """HTTP-клиент сессии в объёме, который трогает sync_statuses (он же контекст-менеджер)."""

    def __enter__(self) -> "_Req":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _chat(vid: int) -> dict[str, Any]:
    return {"chatId": vid, "vacancyId": str(vid), "applicantId": 7,
            "lastMessageTime": REAL_RESPONSE_TS}


def _chat_data(ts: str = REAL_RESPONSE_TS) -> dict[str, Any]:
    """Ответ chat_data с одним сообщением-откликом; ts='' -> чат вообще без сообщений."""
    items = ([{"text": "Отклик", "type": "SIMPLE", "creationTime": ts, "workflowTransitionId": 1}]
             if ts else [])
    return {"chat": {"messages": {"items": items}, "writePossibility": {}},
            "currentApplicantState": "RESPONSE"}


@pytest.fixture
def sync_env(monkeypatch):
    """Все внешние границы синка — заглушками; собираем всё, что он писал на диск."""
    saved: dict[str, list[Any]] = {"statuses": [], "msgs": [], "journal": []}
    monkeypatch.setattr(autoclick.session, "open_client", lambda: (_Req(), "xsrf"))
    monkeypatch.setattr(autoclick, "vacancy_repository",
                        lambda: type("R", (), {"load": staticmethod(list)})())
    monkeypatch.setattr(autoclick.store, "applied_ids", set)
    monkeypatch.setattr(autoclick.store, "chat_messages", dict)
    monkeypatch.setattr(autoclick.store, "statuses", dict)
    monkeypatch.setattr(autoclick.store, "marks", dict)
    monkeypatch.setattr(autoclick.store, "merge_marks", lambda m: None)
    monkeypatch.setattr(autoclick.store, "applied_log", list)
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 0)
    monkeypatch.setattr(autoclick.store, "save_statuses",
                        lambda s: saved["statuses"].append(dict(s)))
    monkeypatch.setattr(autoclick.store, "save_chat_messages",
                        lambda m: saved["msgs"].append(dict(m)))
    monkeypatch.setattr(autoclick.store, "log_applied",
                        lambda vid, name, url, **kw: saved["journal"].append((vid, kw.get("ts"))))
    monkeypatch.setattr(autoclick.time, "sleep", lambda s: None)
    monkeypatch.setattr(autoclick.random, "uniform", lambda a, b: 0)
    return saved


def test_progress_is_checkpointed_before_the_run_ends(sync_env, monkeypatch):
    """250 чатов при чекпойнте раз в 100 -> два промежуточных сброса плюс финальный,
    и первый уже содержит 100 переписок."""
    monkeypatch.setattr(autoclick, "SYNC_CHECKPOINT_EVERY", 100)
    monkeypatch.setattr(autoclick.chat, "list_chats",
                        lambda *a, **k: [_chat(i) for i in range(1, 251)])
    monkeypatch.setattr(autoclick.chat, "chat_data", lambda *a, **k: _chat_data())
    autoclick.sync_statuses()
    assert len(sync_env["msgs"]) == 3
    assert len(sync_env["msgs"][0]) == 100
    assert len(sync_env["msgs"][2]) == 250


def test_kill_between_checkpoints_keeps_the_downloaded_chats(sync_env, monkeypatch):
    """Прогон умирает на 150-м чате: скачанное до чекпойнта на диске, а не в памяти трупа."""
    monkeypatch.setattr(autoclick, "SYNC_CHECKPOINT_EVERY", 100)
    monkeypatch.setattr(autoclick.chat, "list_chats",
                        lambda *a, **k: [_chat(i) for i in range(1, 301)])
    calls = {"n": 0}

    def dying(*a, **k):
        calls["n"] += 1
        if calls["n"] > 150:
            raise KeyboardInterrupt("watchdog/taskkill")
        return _chat_data()

    monkeypatch.setattr(autoclick.chat, "chat_data", dying)
    with pytest.raises(KeyboardInterrupt):
        autoclick.sync_statuses()
    assert len(sync_env["msgs"]) == 1
    assert len(sync_env["msgs"][0]) == 100


def test_chat_without_messages_is_not_journalled_with_todays_date(sync_env, monkeypatch):
    monkeypatch.setattr(autoclick.chat, "list_chats", lambda *a, **k: [_chat(1)])
    monkeypatch.setattr(autoclick.chat, "chat_data", lambda *a, **k: _chat_data(ts=""))
    autoclick.sync_statuses()
    assert sync_env["journal"] == []


def test_chat_with_messages_is_journalled_with_the_real_response_date(sync_env, monkeypatch):
    monkeypatch.setattr(autoclick.chat, "list_chats", lambda *a, **k: [_chat(1)])
    monkeypatch.setattr(autoclick.chat, "chat_data", lambda *a, **k: _chat_data())
    autoclick.sync_statuses()
    assert sync_env["journal"] == [("1", REAL_RESPONSE_TS)]


def test_sync_reconciles_the_daily_quota_after_journalling(sync_env, monkeypatch):
    """Синк — единственный путь, который узнаёт об отклике, не дошедшем до счётчика."""
    got: list[int] = []
    monkeypatch.setattr(autoclick.chat, "list_chats", lambda *a, **k: [_chat(1)])
    monkeypatch.setattr(autoclick.chat, "chat_data", lambda *a, **k: _chat_data())
    monkeypatch.setattr(autoclick, "_journal_applied_today", lambda: 103)
    monkeypatch.setattr(autoclick.store, "reconcile_quota", lambda n: got.append(n) or n)
    autoclick.sync_statuses()
    assert got == [103]
