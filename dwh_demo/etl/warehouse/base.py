"""Порт «хранилище данных». Любой бэкенд (Postgres, ClickHouse, …) реализует
этот контракт и инжектится в Pipeline — конвейер не знает, куда грузит."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain import Vacancy


@runtime_checkable
class Warehouse(Protocol):
    name: str

    def init_schema(self) -> None:
        """Создать/обновить схему (идемпотентно)."""

    def load(self, vacancies: list[Vacancy]) -> int:
        """Загрузить трансформированные вакансии; вернуть число строк в факте."""

    def count(self) -> int:
        """Число вакансий в факте — для верификации."""
