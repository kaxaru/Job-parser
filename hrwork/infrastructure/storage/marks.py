"""Хранилище пользовательских отметок по вакансиям (отклик/отказ).

Источник правды — data/marks.json: { "<vacancy_id>": "applied" | "rejected" }.
Сбор данных файл НЕ трогает, поэтому отметки переживают любой пересбор;
сборка ленты вшивает их в feed.html, а серверный режим — автосохраняет.
"""
import json
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from hrwork.config import DATA_DIR, log

from .filelock import file_lock
from .jsonio import atomic_write_json

MARKS_FILE = DATA_DIR / "marks.json"
# Единственный источник словаря пометок: JS-лента получает его инжектом MARK_VALUES_PY
# (feed.py), НЕ дублирует — иначе разъезжается молча (инцидент "discard" 2026-07-22).
MARK_VALUES = ("applied", "rejected")
_ALLOWED = set(MARK_VALUES)
_SAVE_LOCK = Lock()   # сериализует конкурентные сохранения (ThreadingHTTPServer)
# Межпроцессная блокировка (RFC-004): писателей несколько ПРОЦЕССОВ — сервер, кроны аккаунтов,
# синк. Запись длится миллисекунды, поэтому ожидание дольше таймаута — авария, и писатель
# падает, а не пишет без блокировки: молча потерянный «отказ» дороже упавшего прогона.
MARKS_LOCK_TIMEOUT_S = 30.0


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


def _lock_path() -> Path:
    # от MARKS_FILE в момент вызова: тест, подменивший файл отметок, блокирует свой каталог
    return MARKS_FILE.with_name(MARKS_FILE.name + ".lock")


def update_marks(change: Callable[[dict[str, str]], dict[str, Any]]) -> dict[str, str]:
    """Чтение -> изменение -> запись ЦЕЛИКОМ под блокировкой потоков и процессов.

    Любое изменение отметок, опирающееся на текущее содержимое файла, обязано идти сюда:
    чтение вне блокировки и запись под ней теряют запись, сделанную между ними. Под теми же
    блокировками нет ни гонки за общий tmp-файл, ни частично записанного marks.json."""
    with _SAVE_LOCK, file_lock(_lock_path(), timeout=MARKS_LOCK_TIMEOUT_S):
        clean = {str(k): v for k, v in (change(load_marks()) or {}).items() if v in _ALLOWED}
        atomic_write_json(MARKS_FILE, clean, indent=0)
        return clean
