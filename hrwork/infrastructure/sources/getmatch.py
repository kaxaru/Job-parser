"""Источник getmatch.ru — JSON-API отобранных IT-вакансий (getmatch.ru/api/offers).

Портал маленький (~740 активных против 87k у HH), но данные там ЧИЩЕ: навыки приходят
структурой (`skills_objects`), грейд и формат работы — отдельными полями, а не текстом,
который надо разбирать регексами. Мы всё равно прогоняем их через `build_vacancy`, чтобы
детекция стека/роли осталась единой для всех источников (см. parsing.build_vacancy).

Сбор двухфазный, как у hirify/talanto:
  1) список `/api/offers?offset=&limit=` — карточные поля + короткая выжимка
     `offer_description` (~300-600 симв.);
  2) карточка `/api/offers/{id}` — ПОЛНОЕ описание (`description`, 1.4-4 КБ) плюс `seniority`
     и `required_years_of_experience`, которых в списке НЕТ.
Без второй фазы у вакансии не было бы ни грейда (а по нему идёт отбор под отклик), ни
описания. Дозагрузка инкрементальная: неизменные (по `_sig`) берутся из кеша прошлого сбора.

Авторизация не нужна — API отвечает анонимно (проверено 01.08.2026). Отклики через
Playwright сюда неприменимы: карточка открывается прямой ссылкой.
"""
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    GETMATCH_ENRICH_CONCURRENCY,
    GETMATCH_ENRICH_MAX,
    GETMATCH_PAGE_CONCURRENCY,
    GETMATCH_PAGE_SIZE,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
from .hh import BROWSER_UA

SITE = "https://getmatch.ru"


@dataclass(frozen=True)
class GetmatchCfg:
    api_url: str = f"{SITE}/api/offers"
    page_size: int = GETMATCH_PAGE_SIZE
    max_pages: int = 200                           # страховка: 200 × 100 = 20k, портал в разы меньше
    retry_attempts: int = 4
    backoff_start: float = 1.0
    backoff_max: float = 10.0
    page_conc: int = GETMATCH_PAGE_CONCURRENCY
    enrich_conc: int = GETMATCH_ENRICH_CONCURRENCY
    enrich_max: int = GETMATCH_ENRICH_MAX


CFG = GetmatchCfg()

# Промо-блок «one day offer» приезжает в той же выдаче, но это не вакансия, а анонс
# однодневного мероприятия: у него нет ни грейда, ни зарплаты, ни нормального описания.
_OFFER_TYPE_VACANCY = "vacancy"


def _sig(it: dict[str, Any]) -> str:
    """Маркер изменения карточки: у getmatch правки публикации двигают published_at."""
    return str(it.get("published_at") or "")


def _city(it: dict[str, Any]) -> str:
    """Город из location_requirements (там он разобран), иначе подпись location_items.
    Пусто (полностью удалённая вакансия без привязки) -> 'Remote', как у hirify."""
    for req in it.get("location_requirements") or []:
        if req.get("city"):
            return str(req["city"])
    for li in it.get("location_items") or []:
        if li.get("label"):
            return str(li["label"])
    return REMOTE_CITY


def _formats(it: dict[str, Any]) -> list[str]:
    return [str(li.get("format")) for li in (it.get("location_items") or []) if li.get("format")]


def _skills(it: dict[str, Any]) -> str:
    return " ".join(str(s.get("name")) for s in (it.get("skills_objects") or []) if s.get("name"))


def _salary(it: dict[str, Any]) -> Salary | None:
    """Вилка -> VO. `salary_taxes` = gross|net приходит явно, поэтому НДФЛ считает Salary,
    а не мы. Скрытая вилка (salary_hidden у ~59 %) -> None, как контракт Salary.from_raw."""
    frm, to = it.get("salary_display_from"), it.get("salary_display_to")
    if frm is None and to is None:
        return None
    return Salary(frm, to, it.get("salary_currency"),
                  gross=(it.get("salary_taxes") == "gross"))


def _normalize(it: dict[str, Any], full: dict[str, Any] | None = None,
               cached_desc: str | None = None, enriched_at: str | None = None) -> VacancyRecord:
    """Карточка getmatch -> VacancyRecord (ACL: внешняя схема живёт только здесь).

    `full` — ответ /api/offers/{id}; `cached_desc` — описание из прошлого сбора (тогда сеть
    не трогаем). Грейд берётся из `full`: в списке его нет, поэтому у необогащённых карточек
    experience остаётся None — это честно, лучше пустого поля, чем выдуманный уровень."""
    src = full or it
    if cached_desc is not None:
        desc_html, enriched = cached_desc, True
        at: str | None = enriched_at or storage.now_iso()
    else:
        # description (полный HTML) есть только в карточке; в списке лежит короткая выжимка
        desc_html = str(src.get("description") or "") or str(it.get("offer_description") or "")
        enriched = bool(src.get("description"))
        at = storage.now_iso() if enriched else None

    name = str(it.get("position") or "")
    snippet = str(it.get("offer_description") or "")
    company = it.get("company") or {}
    published = it.get("published_at")
    vac = build_vacancy(
        vid=f"getmatch_{it.get('id')}",         # неймспейс — не сталкивается с id HH/hirify/talanto
        name=name,
        city=_city(it),
        city_id="",
        salary=_salary(it),
        # грейд + годы опыта — только из карточки (в списке полей нет)
        experience=Experience.from_getmatch(src.get("seniority"),
                                            src.get("required_years_of_experience")),
        schedule=Schedule.from_getmatch(_formats(it)),
        # структурные навыки идут в detect_text, а не в обход _detect_techs: единая точка
        # детекции стека для всех источников (parsing.build_vacancy)
        detect_text=f"{name} {_skills(it)} {snippet}",
        employer=str(company.get("name") or ""),
        created_at=published,
        published_at=published,                 # переоткрытия у портала нет — разрыв всегда 0
        responses=None,                         # счётчика откликов API не отдаёт
        source="getmatch",
    )
    return VacancyRecord(vacancy=vac, url=f"{SITE}{it.get('url') or ''}",
                         description_html=desc_html, requirement=snippet,
                         sig=_sig(it), enriched=enriched, enriched_at=at)


@register_source("getmatch")
class GetmatchSource(Source):
    """Сбор вакансий getmatch: пагинация списка + инкрементальная дозагрузка карточек."""

    name = "getmatch"

    def __init__(self, **_: Any) -> None:
        pass                                    # прокси/сессия не нужны — публичный API

    async def _get_json(self, url: str) -> dict[str, Any] | None:
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*",
                   "x-client-platform": "web", "Referer": f"{SITE}/vacancies"}
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace"))
                    return payload
                except json.JSONDecodeError as e:
                    log.debug("getmatch {}: {}", url, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return None

    async def _get_page(self, offset: int) -> dict[str, Any] | None:
        return await self._get_json(
            f"{CFG.api_url}?sa=any&p=1&offset={offset}&limit={CFG.page_size}&pa=all")

    async def _get_one(self, vid: Any) -> dict[str, Any] | None:
        return await self._get_json(f"{CFG.api_url}/{vid}")

    async def collect(self) -> list[VacancyRecord]:
        # Этап 1: первая страница даёт meta.total, остальные offset'ы — параллельно.
        first = await self._get_page(0)
        if not first:
            log.warning("getmatch: первая страница пуста — источник недоступен")
            return []
        total = int((first.get("meta") or {}).get("total") or 0)
        items: list[dict[str, Any]] = list(first.get("offers") or [])
        log.info("getmatch: total={} — тяну список…", total)

        sem = asyncio.Semaphore(CFG.page_conc)

        async def _page(offset: int) -> list[dict[str, Any]]:
            async with sem:
                d = await self._get_page(offset)
            return (d or {}).get("offers") or []

        offsets = list(range(CFG.page_size, min(total, CFG.max_pages * CFG.page_size),
                             CFG.page_size))
        if offsets:
            for chunk in await asyncio.gather(*(_page(o) for o in offsets)):
                items.extend(chunk)

        # Промо-анонсы приезжают в КАЖДОЙ странице, поэтому дедуп по id обязателен, иначе
        # они размножатся по числу страниц (замер: limit=100 -> 104 записи, 4 промо).
        uniq: dict[Any, dict[str, Any]] = {}
        promo = 0
        for it in items:
            if it.get("offer_type") != _OFFER_TYPE_VACANCY:
                promo += 1
                continue
            uniq.setdefault(it.get("id"), it)
        items = list(uniq.values())
        log.info("getmatch: список собран — {} вакансий (промо-анонсов отброшено {})",
                 len(items), promo)

        # Этап 2: инкрементальная дозагрузка карточек (описание + грейд).
        cache = storage.load_desc_cache()
        reuse: list[tuple[dict[str, Any], dict[str, Any]]] = []
        todo: list[dict[str, Any]] = []
        for it in items:
            hit = cache.get(f"getmatch_{it.get('id')}")
            if storage.cache_hit_usable(hit, _sig(it)):
                reuse.append((it, hit))
            else:
                todo.append(it)
        todo.sort(key=lambda it: str(it.get("published_at") or ""), reverse=True)
        to_enrich, list_only = todo[:CFG.enrich_max], todo[CFG.enrich_max:]

        esem = asyncio.Semaphore(CFG.enrich_conc)

        async def _enrich(it: dict[str, Any]) -> VacancyRecord:
            async with esem:
                full = await self._get_one(it.get("id"))
            return _normalize(it, full)

        enriched = list(await asyncio.gather(*(_enrich(it) for it in to_enrich))) if to_enrich else []
        out = [_normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))
               for it, hit in reuse]
        out += enriched
        out += [_normalize(it) for it in list_only]    # без карточки — доберём следующим прогоном
        log.info("getmatch: собрано {} (кеш {}, дозагружено {}, только список {})",
                 len(out), len(reuse), len(enriched), len(list_only))
        return out
