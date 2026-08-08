"""Абстракция источника вакансий (SOLID: Open/Closed + Dependency Inversion).

Коллектор (hh.py::collect) зависит ТОЛЬКО от интерфейса `Source.collect()`; КАК именно
парсится портал (HH: DDoS-Guard/curl/2 стадии/прокси; hirify: JSON-API) — инкапсулировано
в конкретном классе. Новый портал = новый `Source`-подкласс с `@register_source(...)` +
имя в `config.SOURCES`; коллектор не меняется (закрыт для модификации).
"""
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from hrwork.config import log
from hrwork.infrastructure.storage import VacancyRecord

from .hh import HHHtmlClient

T = TypeVar("T")

#: сколько первых сбойных карточек показать подробно — дальше только итоговый счётчик
_ITEM_ERROR_SAMPLE = 3


class Source(ABC):
    """Контракт портала — единственное, что знает коллектор."""

    name: str

    @abstractmethod
    async def collect(self) -> list[VacancyRecord]:
        """Собрать вакансии -> `VacancyRecord` (доменная Vacancy + payload). Адаптер сам
        мапит внешний JSON в домен (ACL), коллектор про схему портала не знает."""
        ...


class ListIncomplete(RuntimeError):
    """Список собран не полностью: часть страниц не отдалась после всех ретраев.

    Бросается вместо возврата усечённого среза. Наверху (`hh.py::_run_source`) это даёт
    `[]` по источнику, а санити-гейт (`hh.py::_degraded_source`) на нулевом срезе НЕ
    перезаписывает кеш — прогон пропускается целиком, данные прошлого сбора остаются.

    Класс ОБЩИЙ для всех постраничных адаптеров. Своя копия в каждом (hirify/talanto/
    getmatch) прожила ровно один заход и была сведена сюда: дубль класса между адаптерами —
    тот же класс инцидента, что дублированные словари грейдов и расписаний."""


def check_list_complete(got: int, *, total: int, per_page: int, failed: list[int],
                        source: str, max_lost_ratio: float,
                        failed_kind: str = "страницы") -> None:
    """Сверка собранного списка с заявленным порталом `total`. Молчать здесь нельзя.

    Страница, не отдавшаяся после всех ретраев, раньше превращалась в ПУСТОЙ кусок и обход
    шёл дальше: транзиентный сбой стоил ~100 записей, прогон отчитывался как успешный
    (потеря меньше 50 %-порога санити-гейта), и усечённый срез затирал кеш — вместе с
    описаниями пропавших вакансий (аудит 08.08.2026, находка 14).

    `max_lost_ratio` — доля, которую можно недобрать молча. Порог ПАРАМЕТР, а не константа:
    он у порталов разный по замеру, обоснование живёт рядом с ним в конфиге адаптера
    (hirify/talanto 2 %, getmatch 25 % — маленький портал, где одна страница это 13 %).
    Выше порога усечённый срез стоит дороже пропуска прогона, ниже — записи вернутся
    следующим сбором.

    `failed` — идентификаторы не отдавшихся страниц, а `failed_kind` называет, ЧТО это:
    hirify нумерует страницы, talanto и getmatch ходят по offset'ам, и оператору нужен тот
    идентификатор, по которому запрос воспроизводится.

    Недобор БЕЗ сбойных страниц (`got < total`) порогом не карается: выдача сдвигается между
    запросами, и расхождение в несколько записей здесь — норма, а не потеря."""
    if not failed:
        if total and got < total:
            log.info("{}: список {} из заявленных {} — выдача сдвинулась между запросами",
                     source, got, total)
        return
    lost = len(failed) * per_page
    share = lost / total if total else 1.0
    head = ", ".join(str(p) for p in sorted(failed)[:5])
    if share >= max_lost_ratio:
        raise ListIncomplete(
            f"страниц не отдалось {len(failed)} (~{lost} записей, {share:.1%} от {total}); "
            f"порог {max_lost_ratio:.0%}; {failed_kind}: {head}")
    log.warning("{}: страниц не отдалось {} (~{} записей, {:.1%} от {}) — срез неполный, "
                "{}: {}", source, len(failed), lost, share, total, failed_kind, head)


def normalize_each(items: Iterable[Any], normalize: Callable[[Any], T | None], *,
                   source: str) -> list[T]:
    """Нормализовать список карточек портала с изоляцией НА ЭЛЕМЕНТЕ.

    «Fail fast на своих ошибках, graceful degradation на чужих»: кривая карточка — чужие
    данные, и ронять из-за неё весь портал нельзя. Без изоляции исключение из `_normalize`
    пробивает до `hh.py::_run_source`, тот отдаёт `[]`, а санити-гейт видит стопроцентную
    просадку и замораживает кеш ВСЕХ порталов до ручного `--force`.

    Мотив: `hirify.py::_meta_header` форматирует `salary_in_usd` через `:,` под truthy-гейтом
    — строковое "120000" из выдачи даёт ValueError на всю страницу.

    `normalize`, вернувший `None`, — это штатный отсев (не подошла вакансия), он в счётчик
    сбоев не идёт.
    """
    out: list[T] = []
    failed = 0
    total = 0
    for it in items:
        total += 1
        try:
            rec = normalize(it)
        except Exception as e:
            failed += 1
            if failed <= _ITEM_ERROR_SAMPLE:
                log.warning("{}: карточка пропущена ({}: {})", source, type(e).__name__, e)
            continue
        if rec is not None:
            out.append(rec)
    if failed:
        log.warning("{}: кривых карточек {} из {} — пропущены", source, failed, total)
    return out


_REGISTRY: dict[str, Callable[..., Source]] = {}


def register_source(name: str) -> Callable[[Callable[..., Source]], Callable[..., Source]]:
    """Декоратор: зарегистрировать фабрику источника (обычно сам класс) по имени."""
    def deco(factory: Callable[..., Source]) -> Callable[..., Source]:
        _REGISTRY[name] = factory
        return factory
    return deco


def get_source(name: str, **kw: Any) -> "Source | None":
    """Экземпляр источника по имени (или None, если не зарегистрирован)."""
    factory = _REGISTRY.get(name)
    return factory(**kw) if factory else None


@register_source("hh")
class HHSource(Source):
    """Адаптер над HHHtmlClient — его curl/анти-бот/2-стадии/прокси остаются приватной
    деталью; наружу только `collect()`."""

    name = "hh"

    def __init__(self, proxies: list[str] | None = None, **_: Any):
        self._proxies = proxies or []

    async def collect(self) -> list[VacancyRecord]:
        client = HHHtmlClient(proxies=self._proxies)
        return await client.collect_all()   # source="hh" проставляется в _record_from_search_item

# Прочие источники (hirify) регистрируются в sources/__init__.py импортом их модулей.
