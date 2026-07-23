"""Источник talanto.work — публичный JSON-API агрегатора (talanto.work/api/jobs).

Список отдаёт постранично (limit/offset, items+total), карточка `/api/jobs/{id}` — полное
HTML-описание и ссылку на первоисточник (телеграм-канал/сайт). Контакты (`/contacts`) —
только под авторизацией, их НЕ трогаем. Нормализуем через `build_vacancy` (ACL) — parse/
Analyzer/views работают без изменений. Отклики (Playwright) неприменимы — карточка
открывается прямой ссылкой; наш `url` ведёт на talanto.work/jobs/{id}.
"""
import asyncio
import html
import json
from dataclasses import dataclass

from hrwork.config import (
    TALANTO_ENRICH_CONCURRENCY,
    TALANTO_ENRICH_MAX,
    TALANTO_PAGE_CONCURRENCY,
    TALANTO_PARAMS,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.parsing import build_vacancy, is_hard_non_it
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
from .hh import BROWSER_UA


@dataclass(frozen=True)
class TalantoCfg:
    api_url: str = "https://talanto.work/api/jobs/"
    page_size: int = 100                            # подтверждено API: limit=100 работает
    max_pages: int = 600                            # страховка (~40k активных / 100 = ~410 страниц)
    retry_attempts: int = 4
    backoff_start: float = 1.0
    backoff_max: float = 10.0
    page_conc: int = TALANTO_PAGE_CONCURRENCY
    enrich_conc: int = TALANTO_ENRICH_CONCURRENCY
    enrich_max: int = TALANTO_ENRICH_MAX


CFG = TalantoCfg()


def _clean_city(loc: str | None) -> str:
    """talanto.location бывает списком стран («Anywhere in the World, 🇦🇩 Andorra, …» до 2400
    симв.) вместо города — это мусор в измерении «город» (ломал верстку дашборда). Берём
    первый сегмент до запятой и режем длину; настоящий город («Москва (м. Киевская)») цел."""
    head = (loc or "").split(",")[0].strip()
    return head[:80]

# Мягкие ACL-маппинги кодов talanto -> VO (неизвестное -> None, дефолт ставит вызывающий —
# контракт «мягкого парсера» из domain.md).
_SCHED = {"remote": Schedule.REMOTE, "hybrid": Schedule.HYBRID, "office": Schedule.OFFICE}
_LEVEL = {"intern": Experience.NONE, "junior": Experience.NONE,
          "mid": Experience.BETWEEN_1_3, "middle": Experience.BETWEEN_1_3,
          "senior": Experience.BETWEEN_3_6, "lead": Experience.MORE_6,
          "principal": Experience.MORE_6, "head": Experience.MORE_6}


def _sig(it: dict) -> str:
    """Маркер изменения для инкрементального enrich (last_verified_at, иначе published_at)."""
    return it.get("last_verified_at") or it.get("published_at") or ""


def _meta_header(it: dict, full: dict | None) -> str:
    """Мета-шапка модалки: уровень · источник вакансии · портал. Тот же осознанный компромисс
    слоёв, что hirify._meta_header (описание собирается на этапе collect и уходит на диск)."""
    parts = []
    if it.get("level"):
        parts.append(f"🎯 {it['level']}")
    src = (full or {}).get("url") or ""
    if src:
        parts.append(f"🔗 первоисточник: {src}")
    parts.append("📌 talanto.work")
    return "<p>" + html.escape(" · ".join(parts)) + "</p>"


def _normalize(it: dict, full: dict | None = None, *,
               cached_desc: str | None = None, enriched_at: str | None = None) -> VacancyRecord:
    """Элемент talanto-API -> VacancyRecord (ACL). full — ответ /api/jobs/{id} с description;
    cached_desc — описание из кеша прошлого сбора (сеть не трогаем)."""
    # Контракт «нет вилки -> None»: без min/max не рождаем Salary-шелуху. gross неизвестен
    # (в текстах встречается «gross», но поля нет) — оставляем как есть (gross=False,
    # НДФЛ не вычитаем повторно; лучше показать заявленное, чем занизить).
    if it.get("salary_min") is not None or it.get("salary_max") is not None:
        salary = Salary(it.get("salary_min"), it.get("salary_max"),
                        it.get("salary_currency"), gross=False)
    else:
        salary = None
    skills = [s for s in (it.get("skills") or []) if s]
    snippet = " ".join(skills)
    if cached_desc is not None:
        desc_html, enriched = cached_desc, True
        at = enriched_at or storage.now_iso()
    else:
        full_desc = (full or {}).get("description")
        desc_html = _meta_header(it, full) + (full_desc or "")
        enriched = bool(full_desc)
        at = storage.now_iso() if enriched else None
    name = it.get("title", "")
    vac = build_vacancy(
        vid=f"talanto_{it.get('id')}",          # неймспейс — не сталкивается с id HH/hirify
        name=name,
        city=_clean_city(it.get("location")),
        city_id="",
        salary=salary,
        experience=_LEVEL.get((it.get("level") or "").lower()),
        schedule=_SCHED.get((it.get("remote_type") or "").lower()) or Schedule.OFFICE,
        detect_text=name + " " + snippet,       # skills в detect_text -> штатный детект стека
        employer=it.get("company") or "",
        created_at=it.get("published_at"),
        published_at=it.get("published_at"),    # переоткрытие не трекается
        responses=None,
        source="talanto",
    )
    return VacancyRecord(vacancy=vac, url=f"https://talanto.work/jobs/{it.get('id', '')}",
                         description_html=desc_html, requirement=snippet,
                         sig=_sig(it), enriched=enriched, enriched_at=at)


@register_source("talanto")
class TalantoSource(Source):
    """Сбор вакансий talanto.work через постраничный JSON-API (limit/offset)."""

    name = "talanto"

    def __init__(self, **_):
        pass                                    # публичный API — прокси/сессия не нужны

    async def _curl_json(self, url: str) -> dict | None:
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "ru"}
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    return json.loads(out.decode("utf-8", "replace"))
                except json.JSONDecodeError as e:
                    log.debug("talanto {}: {}", url, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return None

    async def _get_page(self, offset: int) -> dict | None:
        return await self._curl_json(f"{CFG.api_url}?{TALANTO_PARAMS}&offset={offset}")

    async def _get_one(self, vid: str) -> dict | None:
        """Карточка /api/jobs/{id} — полное HTML-описание + url первоисточника."""
        return await self._curl_json(f"{CFG.api_url}{vid}") if vid else None

    async def collect(self) -> list[VacancyRecord]:
        # Этап 1: offset=0 -> total, дальше страницы параллельно (limit/offset, не page).
        first = await self._get_page(0)
        if not first:
            log.warning("talanto: страница 0 пуста — источник недоступен")
            return []
        items: list[dict] = list(first.get("items") or [])
        total = int(first.get("total") or len(items))
        pages = min((total + CFG.page_size - 1) // CFG.page_size, CFG.max_pages)
        log.info("talanto: total={} страниц={} — тяну список…", total, pages)

        sem = asyncio.Semaphore(CFG.page_conc)

        async def _page(offset: int) -> list[dict]:
            async with sem:
                d = await self._get_page(offset)
            return (d or {}).get("items") or []

        if pages > 1:
            chunks = await asyncio.gather(*(_page(p * CFG.page_size) for p in range(1, pages)))
            for chunk in chunks:
                items.extend(chunk)
        log.info("talanto: список собран — {} вакансий", len(items))

        # Этап 2: инкрементальный enrich описаний (тот же паттерн, что hirify): кеш по _sig,
        # /jobs/{id} только для новых/изменившихся, не-IT тайтлы не обогащаем вовсе
        # (is_hard_non_it ДО enrich — как у HH: не качаем карточки заведомо чужих).
        cache = storage.load_desc_cache()
        reuse: list[tuple[dict, dict]] = []
        todo: list[dict] = []
        for it in items:
            hit = cache.get(f"talanto_{it.get('id')}")
            if storage.cache_hit_usable(hit, _sig(it)):
                reuse.append((it, hit))
            elif not is_hard_non_it(it.get("title") or ""):
                todo.append(it)
        todo.sort(key=lambda it: it.get("published_at") or "", reverse=True)
        to_enrich, rest = todo[:CFG.enrich_max], todo[CFG.enrich_max:]

        esem = asyncio.Semaphore(CFG.enrich_conc)

        async def _enrich(it: dict) -> VacancyRecord:
            async with esem:
                full = await self._get_one(it.get("id", ""))
            return _normalize(it, full)

        enriched = list(await asyncio.gather(*(_enrich(it) for it in to_enrich))) if to_enrich else []
        out = [_normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))
               for it, hit in reuse]
        out += enriched
        out += [_normalize(it) for it in rest]          # без описания — доберём в следующий прогон
        out += [_normalize(it) for it in items
                if f"talanto_{it.get('id')}" not in cache and is_hard_non_it(it.get("title") or "")]
        log.info("talanto: собрано {} (кеш {}, дозагружено {}, отложено {})",
                 len(out), len(reuse), len(enriched), len(rest))
        return out
