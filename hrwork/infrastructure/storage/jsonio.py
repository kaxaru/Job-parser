"""Атомарная запись и безопасное чтение JSON — единый источник для файловых стораджей.

Атомарность: пишем во временный файл рядом и os.replace (атомарен на той же ФС) — крэш
посреди записи не портит рабочий файл. Раньше этот паттерн был скопирован ~6 раз.
"""
import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any, *, indent: int | None = None,
                      ensure_ascii: bool = False) -> None:
    """Записать data в path атомарно (tmp + os.replace). indent=0 -> строка на элемент."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def read_json_or(path: Path, default: Any) -> Any:
    """JSON из path или default (нет файла / битый / тип не совпал с типом default).
    default=None -> тип не проверяем, возвращаем что распарсилось."""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if default is not None and not isinstance(data, type(default)):
        return default
    return data
