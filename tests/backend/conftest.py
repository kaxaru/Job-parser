"""Общая подготовка backend-тестов.

Движки ответов (chat_answer, form_fill) лениво читают `resume_profile.json` из корня —
это персональный файл, в репо его нет (gitignore). На чистом клоне и в CI тесты падали
пустыми ответами (12 штук, вскрыто CI 23.07.2026). Фолбэк: если настоящего профиля нет,
на время сессии подкладывается `resume_profile.example.json` (обезличенный шаблон,
коммитится) и убирается после. Живой профиль разработчика никогда не трогается.
"""
import shutil
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_REAL = _ROOT / "resume_profile.json"
_EXAMPLE = _ROOT / "resume_profile.example.json"


@pytest.fixture(scope="session", autouse=True)
def _resume_profile_fallback():
    if _REAL.exists() or not _EXAMPLE.exists():
        yield                        # профиль есть (или подложить нечего) — ничего не делаем
        return
    shutil.copyfile(_EXAMPLE, _REAL)
    try:
        yield
    finally:
        _REAL.unlink(missing_ok=True)
