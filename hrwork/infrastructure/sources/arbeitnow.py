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
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    ARBEITNOW_MAX_PAGES,
    ARBEITNOW_PAGE_CONCURRENCY,
    log,
)
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import RETRY_STANDARD, RetryPolicy, fetch_json_retry
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, get_page_with_empty_retry, it_only, normalize_each, register_source, warn_if_hole
from .hh import BROWSER_UA
from .text import ts_to_iso

SITE = "https://www.arbeitnow.com"
API = f"{SITE}/api/job-board-api"


@dataclass(frozen=True)
class ArbeitnowCfg:
    api_url: str = API
    max_pages: int = ARBEITNOW_MAX_PAGES
    page_conc: int = ARBEITNOW_PAGE_CONCURRENCY
    # Политика ретраев транспорта — единственный источник значения (net/http.py).
    retry: RetryPolicy = RETRY_STANDARD


CFG = ArbeitnowCfg()


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
    created = ts_to_iso(it.get("created_at"))
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

    async def _fetch_once(self, page: int) -> list[dict[str, Any]]:
        """Одна страница с ретраями ТРАНСПОРТА (сбой curl / битый JSON). Пустой список
        от отвечающего портала здесь не ретраится — это делает `_get_page`."""
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        payload = await fetch_json_retry(
            f"{CFG.api_url}?page={page}", headers=headers, policy=CFG.retry,
            parse=lambda p: p.get("data") or [],
            log_context=f"arbeitnow page={page}")
        return payload or []

    async def _get_page(self, page: int) -> list[dict[str, Any]]:
        """Страница с ПЕРЕПРОВЕРКОЙ пустого ответа — общая `base.get_page_with_empty_retry`
        (семантика и урок инцидента 07.08.2026 живут там, одна копия на три адаптера)."""
        return await get_page_with_empty_retry(
            lambda: self._fetch_once(page), f"page={page}", source="arbeitnow")

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
        exhausted = False
        while page <= CFG.max_pages:
            batch = list(range(page, min(page + CFG.page_conc, CFG.max_pages + 1)))
            chunks = await asyncio.gather(*(self._get_page(p) for p in batch))
            got = [x for c in chunks for x in c]
            items.extend(got)
            read += len(batch)
            if any(not c for c in chunks):       # пусто ПОСЛЕ перепроверок — список кончился
                exhausted = True
                warn_if_hole(batch, chunks, source="arbeitnow")
                break
            page = batch[-1] + 1

        # Одна и та же вакансия может попасть на две страницы, если выдача сдвинулась между
        # запросами (портал отдаёт по дате). Дедуп по slug — он же основа нашего id.
        uniq: dict[str, dict[str, Any]] = {}
        for it in items:
            if it.get("slug"):
                uniq.setdefault(str(it["slug"]), it)
        # Нормализация с изоляцией НА ЭЛЕМЕНТЕ: кривая карточка не должна ронять источник
        # целиком — иначе санити-гейт видит нулевой срез и морозит кеш всех порталов.
        recs = normalize_each(uniq.values(), _normalize, source="arbeitnow")
        # Портал общий, не IT-шный: две трети выдачи — ритейл/логистика/медицина.
        # См. config.GLOBAL_SOURCES_IT_ONLY — там причина и способ выключить.
        out = it_only(recs)
        # `read`, а не `page - 1`: записи последней пачки добавляются ДО выхода из цикла,
        # поэтому счётчик по `page` занижал итог (в логе 37 при реально прочитанных 43).
        log.info("arbeitnow: собрано {} (страниц {}, карточек {}, дублей {}, не-IT отсеяно {})",
                 len(out), read, len(items), len(items) - len(uniq), len(recs) - len(out))
        if not exhausted:
            # Как в himalayas.py: усечение по лимиту выглядит как успешный сбор, и именно
            # молчание превратило там 40 % портала в «полный срез». Портал отдаёт ~41 страницу,
            # так что упереться в лимит = у портала что-то изменилось, а не штатный конец.
            log.warning("arbeitnow: обход УПЁРСЯ В ЛИМИТ {} страниц, выдача НЕ исчерпана — "
                        "часть вакансий не собрана. Поднять: ARBEITNOW_MAX_PAGES", CFG.max_pages)
        return out
