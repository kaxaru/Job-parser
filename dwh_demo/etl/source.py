"""Источник данных. Сейчас один — JSON парсера HH; интерфейс позволяет добавить
другие (API, БД) тем же контрактом read() -> list[dict]."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonSource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        # Чужой JSON — внешняя граница: `json.loads` отдаёт Any, и объявление здесь
        # ОБЕЩАНИЕ формы, а не проверка. Фактическую форму сверяет конвейер
        # (`Pipeline.prepare` отбраковывает элементы, которые не объекты).
        data: list[dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        return data
