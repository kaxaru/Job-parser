"""Межпроцессная запись отметок (RFC-004, R21).

Писателей `marks.json` несколько ПРОЦЕССОВ: сервер ленты, кроны аккаунтов, синк. Без общей
блокировки «прочитал -> изменил -> записал» теряет чужую запись, а потерянная отметка «отказ»
оборачивается необратимым откликом.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hrwork.infrastructure.storage import filelock, marks

ROOT = Path(__file__).resolve().parents[3]

_WRITER = r"""
import sys
from pathlib import Path
from hrwork.infrastructure.storage import marks
marks.MARKS_FILE = Path(sys.argv[1])
prefix, rounds, status = sys.argv[2], int(sys.argv[3]), sys.argv[4]
for i in range(rounds):
    marks.update_marks(lambda cur, i=i: {**cur, f"{prefix}{i}": status})
"""


def test_concurrent_writers_keep_every_mark(tmp_path):
    target = tmp_path / "marks.json"
    writers = [subprocess.Popen([sys.executable, "-c", _WRITER, str(target), prefix, "60", status],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
               for prefix, status in (("a", "applied"), ("r", "rejected"))]
    for w in writers:
        _, err = w.communicate(timeout=120)
        assert w.returncode == 0, err.decode("utf-8", "replace")[-2000:]
    result = json.loads(target.read_text(encoding="utf-8"))
    assert len(result) == 120
    assert [result[f"r{i}"] for i in range(60)] == ["rejected"] * 60
    assert [result[f"a{i}"] for i in range(60)] == ["applied"] * 60


def test_writer_fails_instead_of_writing_without_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(marks, "MARKS_FILE", tmp_path / "marks.json")
    monkeypatch.setattr(marks, "MARKS_LOCK_TIMEOUT_S", 1.0)
    with (filelock.file_lock(tmp_path / "marks.json.lock", timeout=1.0),   # держит «другой писатель»
          pytest.raises(filelock.FileLockTimeout) as err):
        marks.update_marks(lambda cur: {**cur, "1": "rejected"})
    assert str(err.value) == "marks.json.lock: блокировку не дали за 1 с"
    assert not (tmp_path / "marks.json").exists()


def test_lock_is_free_again_after_the_block(tmp_path):
    lock = tmp_path / "x.lock"
    with filelock.file_lock(lock, timeout=1.0):
        pass
    with filelock.file_lock(lock, timeout=0.1):
        entered = True
    assert entered is True
