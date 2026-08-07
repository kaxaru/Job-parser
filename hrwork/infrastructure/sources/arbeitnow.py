"""Источник arbeitnow.com — JSON-API вакансий глобального/европейского рынка.

Первый НЕ-русскоязычный портал в сборе: hh/hirify/talanto/getmatch закрывают рынок РФ,
arbeitnow добавляет удалёнку и релокацию по ЕС. Замер 07.08.2026: 41 страница × 100 = ~4100
вакансий, из них по тайтлу и тегам ~25 Python, ~21 LLM, ~16 backend на первой сотне.

Сбор ОДНОФАЗНЫЙ — в отличие от hirify/getmatch/talanto: полное `description` (HTML) приходит
уже в списке, отдельная карточка не нужна. Поэтому нет ни кеша описаний, ни enrich-лимита.

Чего API НЕ отдаёт и что из этого следует:
  * зарплаты нет вовсе -> `salary=None` у всех записей (в аналитике они не участвуют);
  * грейда нет: `job_types` содержит категории вроде 'professional / experienced', это не
    шкала junior/middle/senior, поэтому `experience=None`. Выдумывать грейд из тайтла нельзя —
    он поедет относительно hirify/getmatch, где грейд приходит полем (см. Experience.from_getmatch).

Авторизация не нужна, ключа нет, лимитов в ответе не заявлено (проверено 07.08.2026).
Автоотклик сюда неприменим: Playwright-путь заточен под форму HH, здесь внешние ссылки.
"""
import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    ARBEITNOW_MAX_PAGES,
    ARBEITNOW_PAGE_CONCURRENCY,
    GLOBAL_SOURCES_IT_ONLY,
    log,
)
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
from .hh import BROWSER_UA

SITE = "https://www.arbeitnow.com"
API = f"{SITE}/api/job-board-api"


@dataclass(frozen=True)
class ArbeitnowCfg:
    api_url: str = API
    max_pages: int = ARBEITNOW_MAX_PAGES
    page_conc: int = ARBEITNOW_PAGE_CONCURRENCY
    retry_attempts: int = 3
    backoff_start: float = 1.0
    backoff_max: float = 8.0


CFG = ArbeitnowCfg()


def _iso(ts: Any) -> str | None:
    """created_at приходит unix-секундами -> ISO-UTC (в остальных источниках дата уже строка)."""
    try:
        return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _sig(it: dict[str, Any]) -> str:
    """Маркер изменения карточки. Своего updated_at у портала нет, поэтому берём дату
    публикации: правка вакансии на arbeitnow создаёт новый slug, а не двигает старый."""
    return str(it.get("created_at") or "")


def _normalize(it: dict[str, Any]) -> VacancyRecord:
    """Карточка arbeitnow -> VacancyRecord (ACL: внешняя схема живёт только здесь)."""
    name = str(it.get("title") or "")
    tags = " ".join(str(t) for t in (it.get("tags") or []))
    jtypes = " ".join(str(t) for t in (it.get("job_types") or []))
    desc = str(it.get("description") or "")
    created = _iso(it.get("created_at"))
    slug = str(it.get("slug") or "")
    vac = build_vacancy(
        vid=f"arbeitnow_{slug}",                 # неймспейс — не сталкивается с id других порталов
        name=name,
        city=str(it.get("location") or "") or REMOTE_CITY,
        city_id="",
        salary=None,                             # вилки в API нет — см. шапку модуля
        experience=None,                         # грейда в API нет — см. шапку модуля
        schedule=Schedule.REMOTE if it.get("remote") else Schedule.OFFICE,
        # описание идёт в детект целиком: у портала нет структурных навыков, и стек
        # угадывается только по тексту вакансии
        detect_text=f"{name} {tags} {jtypes} {desc}",
        employer=str(it.get("company_name") or ""),
        created_at=created,
        published_at=created,                    # переоткрытий у портала нет — разрыв всегда 0
        responses=None,                          # счётчика откликов API не отдаёт
        source="arbeitnow",
    )
    return VacancyRecord(vacancy=vac, url=str(it.get("url") or f"{SITE}/jobs/{slug}"),
                         description_html=desc, requirement=desc[:600],
                         sig=_sig(it), enriched=bool(desc),
                         enriched_at=None)


@register_source("arbeitnow")
class ArbeitnowSource(Source):
    """Сбор вакансий arbeitnow: пагинация списка, одна фаза."""

    name = "arbeitnow"

    def __init__(self, **_: Any) -> None:
        pass                                     # прокси/сессия не нужны — публичный API

    async def _get_page(self, page: int) -> list[dict[str, Any]]:
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(f"{CFG.api_url}?page={page}", headers=headers)
            if out:
                try:
                    payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace"))
                    data: list[dict[str, Any]] = payload.get("data") or []
                    return data
                except json.JSONDecodeError as e:
                    log.debug("arbeitnow page={}: {}", page, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return []

    async def collect(self) -> list[VacancyRecord]:
        first = await self._get_page(1)
        if not first:
            log.warning("arbeitnow: первая страница пуста — источник недоступен")
            return []

        # Общего счётчика (meta.total) API не отдаёт, поэтому идём страницами, пока не
        # упрёмся в пустую. Тянем пачками по page_conc, чтобы не слать 41 запрос последовательно
        # и при этом не долбить портал полусотней разом.
        items: list[dict[str, Any]] = list(first)
        page = 2
        read = 1                                 # реально прочитано страниц (для лога)
        while page <= CFG.max_pages:
            batch = list(range(page, min(page + CFG.page_conc, CFG.max_pages + 1)))
            chunks = await asyncio.gather(*(self._get_page(p) for p in batch))
            got = [x for c in chunks for x in c]
            items.extend(got)
            read += len(batch)
            if any(not c for c in chunks):       # в пачке встретилась пустая — список кончился
                break
            page = batch[-1] + 1

        # Одна и та же вакансия может попасть на две страницы, если выдача сдвинулась между
        # запросами (портал отдаёт по дате). Дедуп по slug — он же основа нашего id.
        uniq: dict[str, dict[str, Any]] = {}
        for it in items:
            if it.get("slug"):
                uniq.setdefault(str(it["slug"]), it)
        recs = [_normalize(it) for it in uniq.values()]
        # Портал общий, не IT-шный: две трети выдачи — ритейл/логистика/медицина.
        # См. config.GLOBAL_SOURCES_IT_ONLY — там причина и способ выключить.
        out = [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs
        # `read`, а не `page - 1`: записи последней пачки добавляются ДО выхода из цикла,
        # поэтому счётчик по `page` занижал итог (в логе 37 при реально прочитанных 43).
        log.info("arbeitnow: собрано {} (страниц {}, карточек {}, дублей {}, не-IT отсеяно {})",
                 len(out), read, len(items), len(items) - len(uniq), len(recs) - len(out))
        return out
