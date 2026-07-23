"""Источник данных. Сейчас один — JSON парсера HH; интерфейс позволяет добавить
другие (API, БД) тем же контрактом read() -> list[dict]."""
from __future__ import annotations

import json
from pathlib import Path


class JsonSource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))
