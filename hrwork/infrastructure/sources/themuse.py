"""Источник themuse.com — публичный JSON-API вакансий (США и глобально).

Самый крупный из добавленных: 407 097 вакансий в общей выдаче против ~97k у himalayas и
~4k у arbeitnow. Ключ не нужен. Ценен ещё и тем, что отдаёт ГРЕЙД полем (`levels`:
entry/mid/senior) — у arbeitnow и web3 его нет вовсе, и там `experience` остаётся пустым.

Два ограничения API определяют схему сбора (замер 07.08.2026):

1. ПОТОЛОК ПАГИНАЦИИ — 99 страниц по 20 = 1980 записей на запрос; страница 100 отдаёт
   HTTP 400. Поэтому охват набирается КОМБИНАЦИЯМИ фильтров `category × level`
   (`THEMUSE_QUERIES`), каждая до 1980, — как перебор тегов у web3.career. Серверные
   фильтры работают и сильно сужают: без фильтра 407k, `category=Software Engineering`
   100 877, плюс `level=Entry Level` — 30 348.

2. СОРТИРОВКИ ПО ДАТЕ НЕТ. `descending=true` порядок по свежести не даёт: на первой
   странице соседствуют февраль и июль, в выдаче попадаются публикации годичной давности.
   Поэтому свежесть режется на НАШЕЙ стороне (`THEMUSE_MAX_AGE_DAYS`) — иначе кеш забьётся
   объявлениями, которые давно закрыты, а `is_ghost` пометит их только post factum.

Сбор однофазный: `contents` (полный HTML) приходит в списке.
Автоотклик неприменим: заявка уходит на сайт работодателя (`refs.landing_page`).
"""
import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    GLOBAL_SOURCES_IT_ONLY,
    THEMUSE_MAX_AGE_DAYS,
    THEMUSE_MAX_PAGES,
    THEMUSE_PAGE_CONCURRENCY,
    THEMUSE_QUERIES,
    log,
)
from hrwork.domain import freshness
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, normalize_each, register_source
from .hh import BROWSER_UA

SITE = "https://www.themuse.com"
API = f"{SITE}/api/public/jobs"
PAGE_SIZE = 20            # задан порталом, не нами
PAGE_CEILING = 99         # страница 100 -> HTTP 400 (проверено 07.08.2026)


@dataclass(frozen=True)
class ThemuseCfg:
    api_url: str = API
    page_size: int = PAGE_SIZE
    max_pages: int = THEMUSE_MAX_PAGES
    page_conc: int = THEMUSE_PAGE_CONCURRENCY
    max_age_days: int = THEMUSE_MAX_AGE_DAYS
    retry_attempts: int = 3
    backoff_start: float = 1.0
    backoff_max: float = 8.0
    # Перепроверка пустой страницы — семантика himalayas.py::_get_page, см. _get_page ниже.
    empty_retries: int = 3
    empty_retry_delay: float = 2.0
    empty_retry_max: float = 8.0


CFG = ThemuseCfg()


def _iso(ts: Any) -> str | None:
    raw = str(ts or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _sig(it: dict[str, Any]) -> str:
    return str(it.get("publication_date") or "")


def _grades(it: dict[str, Any]) -> list[str]:
    """Ярлыки грейда из схемы портала (`levels`: [{name, short_name}])."""
    return [str(x.get("short_name") or x.get("name") or "") for x in (it.get("levels") or [])]


def _city(it: dict[str, Any]) -> str:
    locs = [str(x.get("name") or "") for x in (it.get("locations") or []) if x.get("name")]
    if not locs:
        return REMOTE_CITY
    return ", ".join(locs[:3]) + ("…" if len(locs) > 3 else "")


def _is_remote(it: dict[str, Any]) -> bool:
    """У портала удалёнка выражена локацией «Flexible / Remote», отдельного флага нет."""
    return any("remote" in str(x.get("name") or "").lower()
               or "flexible" in str(x.get("name") or "").lower()
               for x in (it.get("locations") or []))


def _warn_if_hole(query: str, pages: list[int], chunks: list[list[dict[str, Any]]]) -> None:
    """Обход комбинации прерывается на первой пустой странице. Если ПОСЛЕ неё в той же пачке
    страница отдала данные, пустая была ДЫРОЙ, а не концом выдачи: комбинация не исчерпана,
    а обход всё равно остановлен, и хвост за пачкой не собран. Молчать про это нельзя —
    именно молчание превратило троттлинг himalayas в «успешный» сбор 40 % портала."""
    empty = [p for p, c in zip(pages, chunks) if not c]
    last_full = max((p for p, c in zip(pages, chunks) if c), default=0)
    if empty and empty[0] < last_full:
        log.warning("themuse [{}]: обход оборван на ПУСТОЙ странице {}, но страница {} той же "
                    "пачки отдала данные — выдача НЕ кончилась, часть вакансий не собрана",
                    query, empty[0], last_full)


def _normalize(it: dict[str, Any]) -> VacancyRecord:
    """Карточка themuse -> VacancyRecord (ACL: внешняя схема живёт только здесь)."""
    name = str(it.get("name") or "")
    desc = str(it.get("contents") or "")
    cats = " ".join(str(c.get("name") or "") for c in (it.get("categories") or []))
    tags = " ".join(str(t.get("name") or "") for t in (it.get("tags") or []))
    company = it.get("company") or {}
    when = _iso(it.get("publication_date"))
    landing = str((it.get("refs") or {}).get("landing_page") or "")
    vac = build_vacancy(
        vid=f"themuse_{it.get('id')}",           # неймспейс — не сталкивается с другими порталами
        name=name,
        city=_city(it),
        city_id="",
        salary=None,                             # вилки в API нет
        experience=Experience.from_grades(_grades(it)),
        schedule=Schedule.REMOTE if _is_remote(it) else Schedule.OFFICE,
        detect_text=f"{name} {cats} {tags} {desc}",
        employer=str(company.get("name") or ""),
        created_at=when,
        published_at=when,
        responses=None,
        source="themuse",
    )
    return VacancyRecord(vacancy=vac, url=landing or f"{SITE}/jobs/{it.get('short_name') or ''}",
                         description_html=desc, requirement=desc[:600],
                         sig=_sig(it), enriched=bool(desc), enriched_at=None)


@register_source("themuse")
class ThemuseSource(Source):
    """Сбор вакансий themuse: перебор комбинаций фильтров, у каждой свой потолок 99 страниц."""

    name = "themuse"

    def __init__(self, **_: Any) -> None:
        pass

    async def _fetch_once(self, query: str, page: int) -> list[dict[str, Any]]:
        """Одна страница с ретраями ТРАНСПОРТА (сбой curl / битый JSON). Пустой список от
        отвечающего портала здесь не ретраится — это делает `_get_page`."""
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        url = f"{CFG.api_url}?page={page}"
        if query:
            url += "&" + query
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace"))
                    results: list[dict[str, Any]] = payload.get("results") or []
                    return results
                except json.JSONDecodeError as e:
                    log.debug("themuse {} page={}: {}", query, page, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return []

    async def _get_page(self, query: str, page: int) -> list[dict[str, Any]]:
        """Страница с ПЕРЕПРОВЕРКОЙ пустого ответа — та же семантика, что в
        `himalayas.py::_get_page` (3 попытки с паузами 2/4/8 с).

        Урок инцидента 07.08.2026 (himalayas: 12 043 из 26 216 легли в кеш под видом полного
        среза): портал под троттлингом отдаёт HTTP 200 с пустым списком, внешне неотличимый
        от «выдача кончилась», и ретраи транспорта на это не срабатывают — ответ-то пришёл.
        Пришли данные со второй-четвёртой попытки — это был троттлинг; пусто после всех —
        выдача комбинации фильтров действительно кончилась.

        ЦЕНА ОСОЗНАННАЯ: каждая честно пустая страница теперь стоит 2+4+8 = 14 с, и такая
        встречается в конце каждой комбинации `THEMUSE_QUERIES` (их 9) — плюс ~2 минуты к
        прогону. Дешевле подождать, чем недособрать половину портала."""
        results = await self._fetch_once(query, page)
        if results:
            return results
        delay = CFG.empty_retry_delay
        for attempt in range(CFG.empty_retries):
            await asyncio.sleep(delay)
            results = await self._fetch_once(query, page)
            if results:
                log.debug("themuse {} page={}: пусто было троттлингом, ответ с попытки {}",
                          query, page, attempt + 2)
                return results
            delay = min(delay * 2, CFG.empty_retry_max)
        return []

    async def collect(self) -> list[VacancyRecord]:
        if not THEMUSE_QUERIES:
            log.warning("themuse: список запросов пуст — собирать нечего")
            return []

        seen: set[str] = set()
        out: list[VacancyRecord] = []
        raw_total = dupes = stale = dropped = 0
        limit = min(CFG.max_pages, PAGE_CEILING)

        def _one_card(it: dict[str, Any]) -> VacancyRecord | None:
            """Карточка -> запись или None, если она отсеяна штатно (дубль, старьё, не-IT)."""
            nonlocal dupes, stale, dropped
            vid = str(it.get("id") or "")
            if not vid:
                return None
            if vid in seen:
                dupes += 1
                return None
            seen.add(vid)
            # Возраст считает ДОМЕН (freshness.age_days). Своя копия здесь была не
            # просто дублем: она падала TypeError на дате без таймзоны («can't subtract
            # offset-naive and offset-aware»), а исключение отсюда рвёт весь сбор
            # источника. parse_dt защищён от этого и понимает ISO, «Z» и unix-секунды.
            age = freshness.age_days(it.get("publication_date"))
            if CFG.max_age_days and age is not None and age > CFG.max_age_days:
                stale += 1                   # сортировки по дате нет — режем у себя
                return None
            rec = _normalize(it)
            if GLOBAL_SOURCES_IT_ONLY and not rec.vacancy.role.is_it:
                dropped += 1
                return None
            return rec

        def _take(batch: list[dict[str, Any]]) -> None:
            """Нормализация и отсев ПАЧКАМИ: сырые карточки с полным HTML не копятся до
            конца обхода (та же схема, что в himalayas.py после замера на 731 МБ).
            Изоляция НА ЭЛЕМЕНТЕ (normalize_each): кривая карточка не роняет источник."""
            nonlocal raw_total
            raw_total += len(batch)
            out.extend(normalize_each(batch, _one_card, source="themuse"))

        sem = asyncio.Semaphore(CFG.page_conc)

        async def _one(query: str, page: int) -> list[dict[str, Any]]:
            async with sem:
                return await self._get_page(query, page)

        for query in THEMUSE_QUERIES:
            page = 1
            while page <= limit:
                pages = list(range(page, min(page + CFG.page_conc, limit + 1)))
                chunks = await asyncio.gather(*(_one(query, p) for p in pages))
                for c in chunks:
                    _take(c)
                if any(not c for c in chunks):   # пусто ПОСЛЕ перепроверок — фильтр исчерпан
                    _warn_if_hole(query, pages, chunks)
                    break
                page = pages[-1] + 1

        log.info("themuse: собрано {} (запросов {}, карточек {}, дублей {}, "
                 "старше {} дн отсеяно {}, не-IT отсеяно {})",
                 len(out), len(THEMUSE_QUERIES), raw_total, dupes,
                 CFG.max_age_days, stale, dropped)
        return out
