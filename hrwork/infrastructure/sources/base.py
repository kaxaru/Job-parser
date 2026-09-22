"""Абстракция источника вакансий (SOLID: Open/Closed + Dependency Inversion).

Коллектор (hh.py::collect) зависит ТОЛЬКО от интерфейса `Source.collect()`; КАК именно
парсится портал (HH: DDoS-Guard/curl/2 стадии/прокси; hirify: JSON-API) — инкапсулировано
в конкретном классе. Новый портал = новый `Source`-подкласс с `@register_source(...)` +
имя в `config.SOURCES`; коллектор не меняется (закрыт для модификации).

Здесь же — общая механика, которая до аудита 22.09.2026 лежала копиями по адаптерам:
перепроверка пустой страницы (`get_page_with_empty_retry`), диагностика дыры в пачке
(`warn_if_hole`) и IT-фильтр общих бордов (`it_only`/`is_it_only_survivor`).
"""
import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from hrwork.config import GLOBAL_SOURCES_IT_ONLY, log
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


@dataclass(frozen=True)
class EmptyPageRetry:
    """Политика перепроверки ПУСТОЙ страницы (см. `get_page_with_empty_retry`).

    Значения подобраны на инциденте 07.08.2026: троттлинг отпускает за секунды, а честный
    конец выдачи от ожидания не изменится — поэтому 2/4/8 с и не больше.
    """

    probes: int = 3
    delay: float = 2.0
    delay_max: float = 8.0


#: Единственный экземпляр политики на все порталы: значения одинаковы у всех трёх
#: (arbeitnow/himalayas/themuse), и раньше это были три копии полей CFG.
EMPTY_PAGE_RETRY = EmptyPageRetry()


async def get_page_with_empty_retry(fetch: Callable[[], Awaitable[list[T]]], key: str, *,
                                    source: str,
                                    cfg: EmptyPageRetry = EMPTY_PAGE_RETRY) -> list[T]:
    """Страница с ПЕРЕПРОВЕРКОЙ пустого ответа — одна реализация на все порталы.

    Портал под троттлингом отдаёт HTTP 200 с ПУСТЫМ списком, внешне неотличимый от «выдача
    кончилась», и ретраи транспорта на это не срабатывают — ответ-то пришёл. Обход выходил по
    первой такой странице и отчитывался успехом: 07.08.2026 у himalayas в кеш под видом
    полного среза легли 12 043 вакансии из 26 216. Пришли данные со второй-четвёртой попытки —
    это был троттлинг; пусто после всех — честный конец выдачи.

    Лечение обязано жить в ОДНОМ месте: пока копий было три, урок инцидента применялся к
    himalayas и не применялся к arbeitnow/themuse с той же схемой обхода (аудит 22.09.2026).

    На вход идёт ЗАМЫКАНИЕ, а не номер страницы: у arbeitnow и himalayas ключ — страница
    (`page`/`offset`), у themuse к нему добавляется комбинация фильтров, и что именно
    перепросить, знает только адаптер. `key` идёт в debug-строку («пусто было троттлингом,
    ответ с попытки N») и в решении не участвует.
    """
    data = await fetch()
    if data:
        return data
    delay = cfg.delay
    for attempt in range(cfg.probes):
        await asyncio.sleep(delay)
        data = await fetch()
        if data:
            log.debug("{} {}: пусто было троттлингом, ответ с попытки {}",
                      source, key, attempt + 2)
            return data
        delay = min(delay * 2, cfg.delay_max)
    return []


def warn_if_hole(pages: list[int], chunks: list[list[dict[str, Any]]], *,
                 source: str, query: str | None = None) -> None:
    """Обход прерывается на первой пустой странице. Если ПОСЛЕ неё в той же пачке страница
    отдала данные, пустая была ДЫРОЙ, а не концом выдачи: список не кончился, а обход всё
    равно остановлен, и хвост за пачкой не собран. Молчать про это нельзя — именно молчание
    превратило троттлинг himalayas в «успешный» сбор 40 % портала (07.08.2026).

    Это ЕДИНСТВЕННЫЙ сигнал недособранного хвоста у адаптеров с пачечным обходом, поэтому
    условие живёт в одном месте: правка в копии оставляла бы вторую копию молчащей.

    `query` — комбинация фильтров themuse: без неё по логу не понять, в какой из девяти
    выдач оборвался обход.
    """
    empty = [p for p, c in zip(pages, chunks) if not c]
    last_full = max((p for p, c in zip(pages, chunks) if c), default=0)
    if empty and empty[0] < last_full:
        log.warning("{}: обход оборван на ПУСТОЙ странице {}, но страница {} той же "
                    "пачки отдала данные — выдача НЕ кончилась, часть вакансий не собрана",
                    f"{source} [{query}]" if query else source, empty[0], last_full)


def it_only(recs: list[VacancyRecord]) -> list[VacancyRecord]:
    """IT-фильтр общих бордов: у общероссийских/мировых порталов IT — доля выдачи.

    `GLOBAL_SOURCES_IT_ONLY=False` отключает фильтр целиком (см. config: там причина и
    способ выключить). Проверка обязана быть одна на адаптер: пока она была копией в шести
    `collect`, «фильтр забыли в новом адаптере» виделось только глазами по счётчику
    `не-IT отсеяно`, который у каждого свой (аудит 22.09.2026).
    """
    return [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs


def is_it_only_survivor(rec: VacancyRecord) -> bool:
    """Прошла ли ОДНА запись IT-фильтр — поштучный вариант `it_only`.

    Так отсеивают himalayas и themuse: карточка нормализуется сразу, и выбросить не-IT в тот
    же момент дешевле, чем копить её полный HTML описания до конца обхода.
    """
    return rec.vacancy.role.is_it or not GLOBAL_SOURCES_IT_ONLY


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
