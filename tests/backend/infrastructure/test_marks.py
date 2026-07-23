"""Тесты хранилища отметок откликов/отказов: IO + фильтрация невалидных статусов.

MARKS_FILE/DATA_DIR подменяются на tmp_path — реальный data/marks.json не трогаем.
"""
import json

import pytest

from hrwork.infrastructure.storage import marks


@pytest.fixture
def tmp_marks(tmp_path, monkeypatch):
    f = tmp_path / "marks.json"
    monkeypatch.setattr(marks, "MARKS_FILE", f)
    monkeypatch.setattr(marks, "DATA_DIR", tmp_path)
    return f


def test_load_missing_returns_empty(tmp_marks):
    assert marks.load_marks() == {}


def test_save_then_load_roundtrip(tmp_marks):
    marks.save_marks({"1": "applied", "2": "rejected"})
    assert marks.load_marks() == {"1": "applied", "2": "rejected"}


def test_save_filters_invalid_status(tmp_marks):
    marks.save_marks({"1": "applied", "2": "garbage", "3": "rejected"})
    assert marks.load_marks() == {"1": "applied", "3": "rejected"}


def test_load_filters_invalid_status(tmp_marks):
    tmp_marks.write_text(json.dumps({"1": "applied", "2": "weird"}), encoding="utf-8")
    assert marks.load_marks() == {"1": "applied"}


def test_load_bad_json_returns_empty(tmp_marks):
    tmp_marks.write_text("{ это не json", encoding="utf-8")
    assert marks.load_marks() == {}


def test_load_non_dict_returns_empty(tmp_marks):
    tmp_marks.write_text("[1, 2, 3]", encoding="utf-8")
    assert marks.load_marks() == {}


def test_keys_coerced_to_str(tmp_marks):
    marks.save_marks({123: "applied"})        # int-ключ -> строка
    assert marks.load_marks() == {"123": "applied"}
