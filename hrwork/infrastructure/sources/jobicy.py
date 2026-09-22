"""Источник jobicy.com — JSON-API вакансий с полной удалёнкой.

Профильнее arbeitnow и himalayas по формату: портал целиком про remote, и география
приходит отдельным полем `jobGeo` («USA», «UK», «Anywhere», «EMEA», «APAC, Australia»).
Как и у himalayas, это то, чего нет у большинства досок: «remote» на глобальном рынке чаще
всего значит «remote в пределах одной страны», и без этого поля лента забивается вакансиями,
куда откликаться бессмысленно.

Сбор ОДНОФАЗНЫЙ: `jobDescription` (HTML) приходит в списке, отдельной карточки нет.
Пагинации у API тоже нет — есть только `count` (сколько отдать за раз), поэтому охват
набирается перебором индустрий (`JOBICY_INDUSTRIES`), как теги у web3.career.

Авторизация не нужна (проверено 07.08.2026).
Автоотклик неприменим: заявка уходит на сайт работодателя.
"""
import asyncio
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    JOBICY_COUNT,
    JOBICY_INDUSTRIES,
    JOBICY_REQUEST_CONCURRENCY,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import RETRY_STANDARD, RetryPolicy, fetch_json_retry
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, it_only, normalize_each, register_source
from .hh import BROWSER_UA
from .text import iso_to_iso

SITE = "https://jobicy.com"
API = f"{SITE}/api/v2/remote-jobs"


@dataclass(frozen=True)
class JobicyCfg:
    api_url: str = API
    count: int = JOBICY_COUNT
    req_conc: int = JOBICY_REQUEST_CONCURRENCY
    # Политика ретраев транспорта — единственный источник значения (net/http.py).
    retry: RetryPolicy = RETRY_STANDARD


CFG = JobicyCfg()


def _sig(it: dict[str, Any]) -> str:
    return str(it.get("pubDate") or "")


def _grades(it: dict[str, Any]) -> list[str]:
    """Ярлыки грейда из схемы портала. `jobLevel` — строка через запятую («Entry-Level,
    Junior»); что она означает, решает домен (Experience.from_grades)."""
    return [p.strip() for p in str(it.get("jobLevel") or "").split(",") if p.strip()]


def _city(it: dict[str, Any]) -> str:
    """География найма («USA», «EMEA», «APAC, Australia»). «Anywhere» = места нет -> та же
    доменная REMOTE_CITY, что у остальных порталов.

    Раньше «Anywhere» оставалось как есть, «потому что читается». Намеренность устарела:
    подпись города пользователю видна только в карточке, а КЛЮЧОМ она работает в фасете
    городов ленты и в срезе 01_cities — и там это был отдельный бакет того же состояния
    рядом с «Remote»/«Worldwide»/«Удалённо» (замер по кешу 08.08.2026: jobicy «Anywhere» 7
    записей при 12 268 в бакете Remote). Ровно тот дефект, ради которого REMOTE_CITY
    и заводилась. Уточнённая география («EMEA», «Canada, USA») НЕ сводится — там есть
    ограничение найма, и терять его нельзя."""
    geo = " ".join(str(it.get("jobGeo") or "").split())
    if geo.lower() == "anywhere":
        return REMOTE_CITY
    return geo or REMOTE_CITY


def _normalize(it: dict[str, Any]) -> VacancyRecord:
    """Карточка jobicy -> VacancyRecord (ACL: внешняя схема живёт только здесь)."""
    name = str(it.get("jobTitle") or "")
    desc = str(it.get("jobDescription") or "")
    excerpt = str(it.get("jobExcerpt") or "")
    industry = " ".join(str(x) for x in (it.get("jobIndustry") or []))
    when = iso_to_iso(it.get("pubDate"))
    vac = build_vacancy(
        vid=f"jobicy_{it.get('id')}",            # неймспейс — не сталкивается с id других порталов
        name=name,
        city=_city(it),
        city_id="",
        salary=None,                             # вилки в выдаче нет (поля пустые у всех записей)
        experience=Experience.from_grades(_grades(it)),
        schedule=Schedule.REMOTE,                # портал целиком про удалёнку
        detect_text=f"{name} {industry} {excerpt} {desc}",
        employer=str(it.get("companyName") or ""),
        created_at=when,
        published_at=when,
        responses=None,
        source="jobicy",
    )
    return VacancyRecord(vacancy=vac, url=str(it.get("url") or ""),
                         description_html=desc, requirement=excerpt or desc[:600],
                         sig=_sig(it), enriched=bool(desc), enriched_at=None)


@register_source("jobicy")
class JobicySource(Source):
    """Сбор вакансий jobicy: перебор индустрий (пагинации у API нет)."""

    name = "jobicy"

    def __init__(self, **_: Any) -> None:
        pass

    async def _get(self, industry: str) -> list[dict[str, Any]]:
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        url = f"{CFG.api_url}?count={CFG.count}"
        if industry:
            url += f"&industry={industry}"
        jobs = await fetch_json_retry(url, headers=headers, policy=CFG.retry,
                                      parse=lambda p: p.get("jobs") or [],
                                      log_context=f"jobicy industry={industry}")
        return jobs or []

    async def collect(self) -> list[VacancyRecord]:
        sem = asyncio.Semaphore(CFG.req_conc)

        async def _one(ind: str) -> list[dict[str, Any]]:
            async with sem:
                return await self._get(ind)

        chunks = await asyncio.gather(*(_one(i) for i in JOBICY_INDUSTRIES))

        # Индустрии пересекаются (вакансия бывает и в «Software Engineering», и в «DevOps»),
        # плюс запрос без фильтра возвращает часть тех же карточек -> дедуп по id обязателен.
        uniq: dict[Any, dict[str, Any]] = {}
        total = 0
        for chunk in chunks:
            total += len(chunk)
            for it in chunk:
                if it.get("id") is not None:
                    uniq.setdefault(it["id"], it)
        # Нормализация — через normalize_each: кривая карточка портала не должна ронять
        # источник целиком (иначе санити-гейт видит нулевой срез и морозит кеш всех порталов).
        recs = normalize_each(uniq.values(), _normalize, source="jobicy")
        out = it_only(recs)
        log.info("jobicy: собрано {} (индустрий {}, ответов {}, дублей {}, не-IT отсеяно {})",
                 len(out), len(JOBICY_INDUSTRIES), total, total - len(uniq), len(recs) - len(out))
        return out
