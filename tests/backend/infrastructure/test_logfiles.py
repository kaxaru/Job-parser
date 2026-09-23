"""Уборка logs/ (24.09.2026): каталог дорос до 1,2 ГБ.

Два потока копились без предела: логи кронов (`>> logs\\cron_*.log` в .bat — ротации нет
вовсе, `cron_collect.log` 480 МБ) и per-PID логи loguru (`hr_work_<pid>.log`: retention=3
действует в пределах ОДНОГО PID, а PID у каждого запуска новый — 2665 файлов, 701 МБ с 20.07).
"""
import datetime
import os

import pytest

from hrwork.infrastructure.storage import logfiles

_NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)


def _file(path, text, *, days_old=0):
    path.write_text(text, encoding="utf-8")
    ts = (_NOW - datetime.timedelta(days=days_old)).timestamp()
    os.utime(path, (ts, ts))
    return path


# ── ротация лога крона по размеру ────────────────────────────────────────────────────────

def test_cron_log_over_the_limit_moves_to_dot_one(tmp_path):
    log = _file(tmp_path / "cron_collect.log", "x" * 25)
    assert logfiles.rotate_by_size(log, 20) is True
    assert (log.exists(), (tmp_path / "cron_collect.log.1").read_text(encoding="utf-8")) == (False, "x" * 25)


def test_cron_log_under_the_limit_is_left_alone(tmp_path):
    log = _file(tmp_path / "cron_apply.log", "x" * 10)
    assert logfiles.rotate_by_size(log, 20) is False
    assert log.read_text(encoding="utf-8") == "x" * 10


def test_rotation_keeps_one_generation_the_older_one_is_dropped(tmp_path):
    _file(tmp_path / "cron_chat.log.1", "старое")
    log = _file(tmp_path / "cron_chat.log", "y" * 25)
    logfiles.rotate_by_size(log, 20)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cron_chat.log.1"]
    assert (tmp_path / "cron_chat.log.1").read_text(encoding="utf-8") == "y" * 25


def test_log_held_by_another_process_is_skipped_not_fatal(tmp_path, monkeypatch):
    """Windows не даёт переименовать файл, который держит другой процесс (WinError 32): в 12:00
    рядом со сбором стартует hh_chat и пишет свой лог. Прогон уборки от этого не падает —
    файл уедет на следующий день."""
    log = _file(tmp_path / "cron_chat.log", "z" * 25)

    def held(*_a, **_k):
        raise PermissionError(32, "The process cannot access the file")
    monkeypatch.setattr(os, "replace", held)
    assert logfiles.rotate_by_size(log, 20) is False
    assert log.read_text(encoding="utf-8") == "z" * 25


# ── чистка per-PID логов loguru по возрасту ──────────────────────────────────────────────

def test_process_logs_older_than_the_age_limit_are_deleted(tmp_path):
    _file(tmp_path / "hr_work_1.log", "a", days_old=20)
    _file(tmp_path / "hr_work_2.2026-07-23_21-17-31_272985.log", "b", days_old=20)   # ротированный loguru
    _file(tmp_path / "hr_work_3.log", "c", days_old=1)
    _file(tmp_path / "cron_collect.log", "d", days_old=20)       # не per-PID лог — не наше дело
    deleted = logfiles.prune_process_logs(tmp_path, datetime.timedelta(days=14), now=_NOW)
    assert deleted == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cron_collect.log", "hr_work_3.log"]


def test_process_log_held_open_is_skipped(tmp_path, monkeypatch):
    """Живой процесс держит свой hr_work_<pid>.log открытым: удаление на Windows падает —
    пропускаем, счётчик его не включает."""
    _file(tmp_path / "hr_work_1.log", "a", days_old=20)
    _file(tmp_path / "hr_work_2.log", "b", days_old=20)
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        if str(path).endswith("hr_work_1.log"):
            raise PermissionError(32, "busy")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)
    assert logfiles.prune_process_logs(tmp_path, datetime.timedelta(days=14), now=_NOW) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hr_work_1.log"]


# ── один проход по каталогу ───────────────────────────────────────────────────────────────

def test_housekeeping_rotates_only_live_cron_logs_and_prunes_process_logs(tmp_path):
    _file(tmp_path / "cron_collect.log", "x" * 30)
    _file(tmp_path / "cron_apply.log", "x" * 5)
    _file(tmp_path / "cron_chat.log.1", "x" * 30)          # уже ротированный — не трогаем
    _file(tmp_path / "hr_work_7.log", "x", days_old=30)
    result = logfiles.housekeep(tmp_path, max_bytes=20, max_age=datetime.timedelta(days=14), now=_NOW)
    assert result == logfiles.Housekeeping(rotated=("cron_collect.log",), pruned=1)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "cron_apply.log", "cron_chat.log.1", "cron_collect.log.1"]


@pytest.mark.parametrize(("name", "expected"), [
    ("MAX_CRON_LOG_BYTES", 20 * 1024 * 1024),
    ("PROCESS_LOG_MAX_AGE", datetime.timedelta(days=14)),
])
def test_default_limits(name, expected):
    """Потолки: лог крона ~40 МБ (живой + .1), per-PID логи — две недели (это персональный
    сток: фрагменты переписки, держать его месяцами незачем)."""
    assert getattr(logfiles, name) == expected
