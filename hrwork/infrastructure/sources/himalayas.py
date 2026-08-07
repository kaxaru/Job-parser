"""Источник himalayas.app — JSON-API вакансий с ПОЛНОЙ удалёнкой (global remote).

Второй не-русскоязычный портал после arbeitnow. Данные богаче: приходят грейд (`seniority`),
вилка с валютой и периодом, тип занятости и — главное — `locationRestrictions`: список стран,
из которых работодатель готов нанимать. Это ровно то, чего нет больше нигде: «remote» на
глобальном рынке в большинстве случаев значит «remote в пределах США», и без этого поля лента
забивается вакансиями, куда откликаться бессмысленно. Пишем его в город.

Сбор ОДНОФАЗНЫЙ: `description` (HTML) приходит в списке. Пагинация через `offset`, страница
жёстко 20 записей — `limit` больше 20 портал игнорирует (проверено 07.08.2026), поэтому
страниц много и они тянутся пачками.

ЗАРПЛАТА ПРИВОДИТСЯ К МЕСЯЧНОЙ доменной фабрикой `Salary.monthly`: у портала `salaryPeriod`
= annual (88 %), hourly (9 %), monthly (3 %), а вся вилка в домене — месячная. Адаптер лишь
достаёт поля и переводит период в `SalaryPeriod`; сам пересчёт и пороги живут в домене.

Авторизация не нужна (проверено 07.08.2026). Автоотклик неприменим: заявка уходит на сайт
работодателя по `applicationLink`.
"""
import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    GLOBAL_SOURCES_IT_ONLY,
    HIMALAYAS_BATCH_PAUSE,
    HIMALAYAS_MAX_PAGES,
    HIMALAYAS_PAGE_CONCURRENCY,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary, SalaryPeriod
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
from .hh import BROWSER_UA

SITE = "https://himalayas.app"
API = f"{SITE}/jobs/api"
PAGE_SIZE = 20            # портал игнорирует limit>20 — размер страницы задан им, не нами


@dataclass(frozen=True)
class HimalayasCfg:
    api_url: str = API
    page_size: int = PAGE_SIZE
    max_pages: int = HIMALAYAS_MAX_PAGES
    page_conc: int = HIMALAYAS_PAGE_CONCURRENCY
    # Перепроверка пустой страницы — см. _get_page. Три попытки с 2/4/8 с: троттлинг
    # отпускает за секунды, а конец выдачи от ожидания не изменится.
    empty_retries: int = 3
    empty_retry_delay: float = 2.0
    empty_retry_max: float = 8.0
    batch_pause: float = HIMALAYAS_BATCH_PAUSE


CFG = HimalayasCfg()


def _iso(ts: Any) -> str | None:
    """pubDate приходит unix-секундами -> ISO-UTC."""
    try:
        return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _sig(it: dict[str, Any]) -> str:
    return str(it.get("pubDate") or "")


def _vid(it: dict[str, Any]) -> str:
    """id из guid — это URL вида .../companies/{c}/jobs/{slug}-{number}. Берём хвост пути:
    он уникален и короче полного URL, который иначе поехал бы в ключ кеша целиком."""
    guid = str(it.get("guid") or "").rstrip("/")
    return f"himalayas_{guid.rsplit('/', 1)[-1]}" if guid else ""


def _salary(it: dict[str, Any]) -> Salary | None:
    """Вилка -> VO, приведённая к МЕСЯЧНОЙ доменной фабрикой.

    Адаптер только ДОСТАЁТ поля и переводит `salaryPeriod` в VO; сам пересчёт (annual/12,
    hourly×160) и пороги живут в домене — иначе, как было до 07.08.2026, у каждого портала
    заводится своя копия правила и они расходятся.
    gross=False: портал не размечает налоги, а вычитать 13 % НДФЛ из зарубежной вилки было
    бы враньём — там своя налоговая система."""
    return Salary.monthly(it.get("minSalary"), it.get("maxSalary"), it.get("currency"),
                          SalaryPeriod.from_code(it.get("salaryPeriod")))


def _employer(it: dict[str, Any]) -> str:
    """Работодатель из `companySlug`, а НЕ из `companyName`.

    У портала `companyName` приходит литеральной ЗАГЛУШКОЙ — строкой `"name"` во всех записях
    (как и `companyLogo` = `"thumbnail_url"`); настоящее значение лежит только в слаге.
    Молча взять `companyName` было бы порчей данных: ключ кросс-портальной дедупликации —
    пара (работодатель, тайтл) (см. domain/dedup.py), и одинаковый «name» у 5758 записей
    схлопнул бы разные компании с совпадающим тайтлом в одну вакансию.

    Слаг разворачиваем обратно в имя: bright-vision-technologies -> Bright Vision Technologies.
    Точного имени это не восстановит (регистр аббревиатур и знаки препинания теряются), но
    для подписи в ленте и для ключа дедупа стабильно и различимо."""
    slug = str(it.get("companySlug") or "").strip()
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def _city(it: dict[str, Any]) -> str:
    """Страны, из которых работодатель готов нанимать. Пусто = ограничений нет (нанимают
    откуда угодно) — это ЛУЧШИЙ случай для нас, поэтому он получает явную подпись."""
    locs = [str(x) for x in (it.get("locationRestrictions") or []) if x]
    if not locs:
        return REMOTE_CITY
    return ", ".join(locs[:3]) + ("…" if len(locs) > 3 else "")


def _normalize(it: dict[str, Any]) -> VacancyRecord:
    """Карточка himalayas -> VacancyRecord (ACL: внешняя схема живёт только здесь)."""
    name = str(it.get("title") or "")
    desc = str(it.get("description") or "")
    excerpt = str(it.get("excerpt") or "")
    cats = " ".join(str(c) for c in (it.get("categories") or []))
    pub = _iso(it.get("pubDate"))
    vac = build_vacancy(
        vid=_vid(it),
        name=name,
        city=_city(it),
        city_id="",
        salary=_salary(it),
        experience=Experience.from_grades(it.get("seniority") or []),
        schedule=Schedule.REMOTE,                # портал целиком про удалёнку
        detect_text=f"{name} {cats} {excerpt} {desc}",
        employer=_employer(it),                  # НЕ companyName — там заглушка, см. _employer
        created_at=pub,
        published_at=pub,
        responses=None,
        source="himalayas",
    )
    return VacancyRecord(vacancy=vac, url=str(it.get("applicationLink") or it.get("guid") or ""),
                         description_html=desc, requirement=excerpt or desc[:600],
                         sig=_sig(it), enriched=bool(desc), enriched_at=None)


@register_source("himalayas")
class HimalayasSource(Source):
    """Сбор вакансий himalayas: пагинация по offset, одна фаза."""

    name = "himalayas"

    def __init__(self, **_: Any) -> None:
        pass

    async def _fetch_once(self, offset: int) -> list[dict[str, Any]]:
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        out = await fetch_bytes(f"{CFG.api_url}?limit={CFG.page_size}&offset={offset}",
                                headers=headers)
        if not out:
            return []
        try:
            payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace"))
            jobs: list[dict[str, Any]] = payload.get("jobs") or []
            return jobs
        except json.JSONDecodeError as e:
            log.debug("himalayas offset={}: {}", offset, e)
            return []

    async def _get_page(self, offset: int) -> list[dict[str, Any]]:
        """Страница с ПЕРЕПРОВЕРКОЙ пустого ответа.

        Портал притормаживает после ~1900 запросов подряд и начинает отдавать пустой список,
        внешне неотличимый от «выдача кончилась». Обход выходил по первой пустой странице и
        молча обрывался на 40 % (замер 07.08.2026: встал на offset 38 900, тогда как
        одиночные пробы сразу после этого отдавали данные вплоть до 95 000, и только
        97 400 был честно пуст). Отчитывался при этом как об успехе.

        Поэтому пустой ответ — не приговор: ждём и повторяем. Пришли данные — это был
        троттлинг; пусто и после всех попыток — конец выдачи."""
        jobs = await self._fetch_once(offset)
        if jobs:
            return jobs
        delay = CFG.empty_retry_delay
        for attempt in range(CFG.empty_retries):
            await asyncio.sleep(delay)
            jobs = await self._fetch_once(offset)
            if jobs:
                log.debug("himalayas offset={}: пусто было троттлингом, ответ с попытки {}",
                          offset, attempt + 2)
                return jobs
            delay = min(delay * 2, CFG.empty_retry_max)
        return []

    async def collect(self) -> list[VacancyRecord]:
        first = await self._get_page(0)
        if not first:
            log.warning("himalayas: первая страница пуста — источник недоступен")
            return []

        # Записи обрабатываются ПАЧКАМИ, а не копятся сырыми до конца обхода. Разница
        # принципиальная для глубины: сырая карточка тащит полный HTML описания, и на
        # 2000 страницах накопление занимало 731 МБ (замер 07.08.2026) — на полной глубине
        # (~4870 страниц) это было бы ~1.8 ГБ, поэтому лимит и стоял. Нормализуя и отсеивая
        # не-IT сразу, в памяти держим только выживших: их 37 % от выдачи.
        seen: set[str] = set()
        out: list[VacancyRecord] = []
        raw_total = dupes = dropped = 0

        def _take(batch: list[dict[str, Any]]) -> None:
            nonlocal raw_total, dupes, dropped
            for it in batch:
                raw_total += 1
                vid = _vid(it)
                if not vid:
                    continue
                if vid in seen:                  # выдача сортируется по дате и сдвигается
                    dupes += 1                   # между запросами -> окна пересекаются
                    continue
                seen.add(vid)
                rec = _normalize(it)
                if GLOBAL_SOURCES_IT_ONLY and not rec.vacancy.role.is_it:
                    dropped += 1
                    continue
                out.append(rec)

        _take(first)
        page = 1
        exhausted = False
        while page < CFG.max_pages:
            offsets = [(page + i) * CFG.page_size
                       for i in range(CFG.page_conc)
                       if page + i < CFG.max_pages]
            chunks = await asyncio.gather(*(self._get_page(o) for o in offsets))
            for c in chunks:
                _take(c)
            if any(not c for c in chunks):       # пусто ПОСЛЕ перепроверок — выдача кончилась
                exhausted = True
                break
            page += len(offsets)
            # Пауза между пачками: без неё портал уходил в троттлинг примерно на 1900-м
            # запросе. Дешевле подождать, чем недособрать 60 % выдачи.
            await asyncio.sleep(CFG.batch_pause)

        log.info("himalayas: собрано {} (страниц {}, карточек {}, дублей {}, не-IT отсеяно {})",
                 len(out), page, raw_total, dupes, dropped)
        if not exhausted:
            # Молчать здесь нельзя: усечение выглядит как успешный сбор, и именно так
            # 07.08.2026 в кеш попало 40 % портала под видом полного среза.
            log.warning("himalayas: обход УПЁРСЯ В ЛИМИТ {} страниц, выдача НЕ исчерпана — "
                        "часть вакансий не собрана. Поднять: HIMALAYAS_MAX_PAGES",
                        CFG.max_pages)
        return out
