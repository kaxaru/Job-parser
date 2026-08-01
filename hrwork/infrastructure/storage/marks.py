"""Хранилище пользовательских отметок по вакансиям (отклик/отказ).

Источник правды — data/marks.json: { "<vacancy_id>": "applied" | "rejected" }.
Сбор данных файл НЕ трогает, поэтому отметки переживают любой пересбор;
сборка ленты вшивает их в feed.html, а серверный режим — автосохраняет.
"""
import json
from threading import Lock
from typing import Any

from hrwork.config import DATA_DIR, log

from .jsonio import atomic_write_json

MARKS_FILE = DATA_DIR / "marks.json"
# Единственный источник словаря пометок: JS-лента получает его инжектом MARK_VALUES_PY
# (feed.py), НЕ дублирует — иначе разъезжается молча (инцидент "discard" 2026-07-22).
MARK_VALUES = ("applied", "rejected")
_ALLOWED = set(MARK_VALUES)
_SAVE_LOCK = Lock()   # сериализует конкурентные сохранения (ThreadingHTTPServer)


def load_marks() -> dict[str, str]:
    if not MARKS_FILE.exists():
        return {}
    try:
        data = json.loads(MARKS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("marks.json не прочитан ({}), считаем пустым", e)
        return {}
    if not isinstance(data, dict):
        return {}
    # отбрасываем мусор, оставляем только валидные статусы
    return {str(k): v for k, v in data.items() if v in _ALLOWED}


def save_marks(marks: dict[str, Any]) -> None:
    clean = {str(k): v for k, v in (marks or {}).items() if v in _ALLOWED}
    # Лок сериализует конкурентные сохранения (ThreadingHTTPServer) -> ни гонки за tmp,
    # ни частично записанного marks.json при краше (источник правды не бьётся).
    with _SAVE_LOCK:
        atomic_write_json(MARKS_FILE, clean, indent=0)
