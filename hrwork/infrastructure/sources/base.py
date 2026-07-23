"""Абстракция источника вакансий (SOLID: Open/Closed + Dependency Inversion).

Коллектор (hh.py::collect) зависит ТОЛЬКО от интерфейса `Source.collect()`; КАК именно
парсится портал (HH: DDoS-Guard/curl/2 стадии/прокси; hirify: JSON-API) — инкапсулировано
в конкретном классе. Новый портал = новый `Source`-подкласс с `@register_source(...)` +
имя в `config.SOURCES`; коллектор не меняется (закрыт для модификации).
"""
from abc import ABC, abstractmethod
from collections.abc import Callable

from hrwork.infrastructure.storage import VacancyRecord

from .hh import HHHtmlClient


class Source(ABC):
    """Контракт портала — единственное, что знает коллектор."""

    name: str

    @abstractmethod
    async def collect(self) -> list[VacancyRecord]:
        """Собрать вакансии -> `VacancyRecord` (доменная Vacancy + payload). Адаптер сам
        мапит внешний JSON в домен (ACL), коллектор про схему портала не знает."""
        ...


_REGISTRY: dict[str, Callable[..., Source]] = {}


def register_source(name: str):
    """Декоратор: зарегистрировать фабрику источника (обычно сам класс) по имени."""
    def deco(factory: Callable[..., Source]):
        _REGISTRY[name] = factory
        return factory
    return deco


def get_source(name: str, **kw) -> "Source | None":
    """Экземпляр источника по имени (или None, если не зарегистрирован)."""
    factory = _REGISTRY.get(name)
    return factory(**kw) if factory else None


@register_source("hh")
class HHSource(Source):
    """Адаптер над HHHtmlClient — его curl/анти-бот/2-стадии/прокси остаются приватной
    деталью; наружу только `collect()`."""

    name = "hh"

    def __init__(self, proxies: list[str] | None = None, **_):
        self._proxies = proxies or []

    async def collect(self) -> list[VacancyRecord]:
        client = HHHtmlClient(proxies=self._proxies)
        return await client.collect_all()   # source="hh" проставляется в _record_from_search_item

# Прочие источники (hirify) регистрируются в sources/__init__.py импортом их модулей.
