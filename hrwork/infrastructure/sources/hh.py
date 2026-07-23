"""Сбор вакансий через публичные HTML-страницы hh.ru (API закрыт DDoS-Guard).

Двухэтапно:
  1) страницы поиска hh.ru/search/vacancy -> список вакансий со структурой
     (зарплата, город, опыт, формат работы, ссылка);
  2) карточка hh.ru/vacancy/<id> -> описание + keySkills для детекции стека.

На выходе — raw-словари в том же формате, что отдавал старый API,
поэтому parse_vacancy/Analyzer/views работают без изменений.
"""
import asyncio
import html as _html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from hrwork.config import (
    CITIES,
    CONCURRENCY,
    HH_ENRICH_BATCH_MULT,
    MAX_PAGES,
    PAGE_DELAY,
    PER_PAGE,
    SEARCH_QUERIES,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.parsing import build_vacancy, is_hard_non_it
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord


# ── Конфиг сбора HH (транспорт). Тяжелее hirify (DDoS-Guard) — больше попыток, выше потолок паузы. ──
@dataclass(frozen=True)
class HhCfg:
    base: str = "https://hh.ru"
    retry_attempts: int = 5
    backoff_start: float = 1.0
    backoff_max: float = 20.0
    enrich_log_every: int = 500    # прогресс-лог enrich: раз в N обработанных карточек


CFG = HhCfg()
SEARCH_URL = f"{CFG.base}/search/vacancy"
# UA общий (импортируется hirify и autoclick) — оставляем на уровне модуля.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_STATE_RE = re.compile(
    r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_state(html: str) -> dict | None:
    m = _STATE_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(_html.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", s)).strip()


def _work_formats(item: dict) -> list[str]:
    out: list[str] = []
    for wf in item.get("workFormats") or []:
        out.extend(wf.get("workFormatsElement") or [])
    return out


def _record_from_search_item(item: dict, *, city: str, city_id: str) -> VacancyRecord:
    """Элемент поиска HH -> VacancyRecord (ACL: внешний JSON СРАЗУ в домен, без raw-dict).
    Город берём из ПОИСКА (city/area_id), а не из area вакансии — так вакансия числится за
    городом, под которым найдена. techs/role посчитаются лишь по тайтлу (requirement пуст) —
    достроятся в _enrich, когда придёт текст карточки (стадия 2)."""
    comp = item.get("compensation") or {}
    salary = None
    # is None, не truthiness: вилка from=0 — валидная граница (тот же контракт, что Salary.from_raw)
    if comp.get("from") is not None or comp.get("to") is not None:
        salary = Salary(comp.get("from"), comp.get("to"),
                        comp.get("currencyCode"), gross=bool(comp.get("gross"))).net()
    # Тайминг вакансии (есть ТОЛЬКО в поисковой выдаче, не на карточке):
    #   creationTime — когда реально создана; publicationTime — когда последний раз поднята.
    # Большой разрыв creation<->publication = «висит и переоткрывается» (гост-вакансия).
    pub = item.get("publicationTime") or {}
    name = item.get("name", "")
    vac = build_vacancy(
        vid=str(item.get("vacancyId")), name=name,
        city=city, city_id=city_id,
        salary=salary,
        experience=Experience.from_code(item.get("workExperience", "")),
        schedule=Schedule.from_hh_formats(_work_formats(item)),
        detect_text=name,                                       # requirement добавится в _enrich
        employer=(item.get("company") or {}).get("visibleName", ""),
        created_at=item.get("creationTime"),                    # ISO "2026-04-30T08:20:03+03:00"
        published_at=pub.get("$") or pub.get("@timestamp"),     # ISO или unix-секунды
        responses=item.get("totalResponsesCount"),              # уже откликнулось (конкуренция)
        source="hh",
    )
    # sig — маркер изменения для инкрементального enrich: переоткрытие бампит publicationTime
    sig = pub.get("$") or pub.get("@timestamp") or item.get("creationTime") or ""
    return VacancyRecord(vacancy=vac, url=(item.get("links") or {}).get("desktop", ""),
                         sig=sig)                               # enriched=False, описание пусто


def _rebuild_techs(rec: VacancyRecord) -> None:
    """Пере-собрать techs/role из ПОЛНОГО текста карточки (тайтл + requirement), сохранив
    прочие доменные поля. Нужно, т.к. в стадии 1 requirement пуст (стек в карточке, стадия 2)."""
    v = rec.vacancy
    rec.vacancy = build_vacancy(
        vid=v.id, name=v.name, city=v.city, city_id=v.city_id, salary=v.salary,
        experience=v.experience, schedule=v.schedule,
        detect_text=v.name + " " + rec.requirement,
        employer=v.employer, created_at=v.created_at, published_at=v.published_at,
        responses=v.responses, source=v.source,
    )


class HHHtmlClient:
    """Сетевой слой на curl-подпроцессах.

    aiohttp в долгоживущем процессе через VPN-туннель на Windows стабильно
    падает с WinError 64 (соединение сброшено), тогда как curl на тех же
    запросах отдаёт 200. Поэтому каждый GET — отдельный процесс curl.
    """

    def __init__(self, proxies: list[str] | None = None):
        # Ресурсов, требующих закрытия, нет (curl — процесс на запрос; httpx-пул живёт в
        # net.http): клиент создаётся обычным конструктором, без context manager.
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.proxies = proxies or []
        self._rr = 0  # round-robin указатель по списку прокси

    def _next_proxy(self) -> str | None:
        if not self.proxies:
            return None
        p = self.proxies[self._rr % len(self.proxies)]
        self._rr += 1
        return p

    async def _curl(self, url: str) -> str | None:
        headers = {"User-Agent": BROWSER_UA, "Accept-Language": "ru-RU,ru;q=0.9"}
        out = await fetch_bytes(url, headers=headers, proxy=self._next_proxy())
        return out.decode("utf-8", "replace") if out else None

    async def _get_state(self, url: str, retries: int = CFG.retry_attempts) -> dict | None:
        """200 + встроенный JSON или None после ретраев с бэкоффом. Ретрай не только по сбою
        curl, но и по «пусто/нет state» — так ловим soft-блок DDoS-Guard (HTML без данных)."""
        delay = CFG.backoff_start
        for attempt in range(retries):
            async with self.sem:
                html = await self._curl(url)
            if html:
                state = _extract_state(html)
                if state is not None:
                    return state
            log.debug("{} попытка {}: пусто/ошибка", url, attempt)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return None

    async def _search_page(self, area_id: str, page: int, query: str) -> list[dict]:
        url = SEARCH_URL + "?" + urlencode({
            "text": query, "area": area_id,
            "page": page, "items_on_page": PER_PAGE,
        })
        state = await self._get_state(url)
        if state is None:
            raise RuntimeError("блок/сеть после ретраев")
        return (state.get("vacancySearchResult") or {}).get("vacancies") or []

    async def _fetch_detail(self, vac_id: str) -> tuple[str, str]:
        """Карточка -> (текст для детекции стека, HTML-описание для показа).

        Текст = keySkills + очищенное описание; HTML — исходное описание hh
        (с разметкой) для рендера в ленте.
        """
        try:
            state = await self._get_state(f"{CFG.base}/vacancy/{vac_id}")
        except Exception as e:
            log.debug("Карточка {} не получена: {}", vac_id, e)
            return "", ""
        view = (state or {}).get("vacancyView") or {}
        skills = (view.get("keySkills") or {}).get("keySkill") or []
        desc_html = view.get("description") or ""
        await asyncio.sleep(PAGE_DELAY)
        techs_text = " ".join(skills) + " " + _strip_html(desc_html)
        return techs_text, desc_html

    async def _enrich(self, rec: VacancyRecord):
        text, desc_html = await self._fetch_detail(rec.vacancy.id)
        if not (text.strip() or desc_html):
            return   # сбой сети/блок — не затираем ранее добытые описания
        rec.requirement = text
        rec.description_html = desc_html
        rec.enriched = True
        rec.enriched_at = storage.now_iso()              # реальная дозагрузка -> метка времени
        _rebuild_techs(rec)                              # техи/роль из полного текста карточки

    async def run_enrich(self, records: list[VacancyRecord], skip_filled: bool = False):
        """Дозагрузка карточек пачками (публичный: зовёт и collect_all, и CLI-режим enrich).
        Инкрементально: неизменные (по sig) берём из кеша прошлого сбора без сети;
        /vacancy/<id> тянем только для новых/переоткрытых.
        skip_filled — добирать только пустые (ручной режим enrich)."""
        cache = storage.load_desc_cache()               # {id: {sig, description_html, requirement}}
        reused = 0
        todo: list[VacancyRecord] = []
        for r in records:
            hit = cache.get(r.vacancy.id)
            if storage.cache_hit_usable(hit, r.sig):
                r.description_html = hit["description_html"]        # из кеша, без сети
                r.requirement = hit["requirement"]
                r.enriched = True
                r.enriched_at = hit.get("at") or storage.now_iso()  # переносим метку дозагрузки
                _rebuild_techs(r)                                   # техи из кешированного текста
                reused += 1
                continue
            if skip_filled and r.requirement.strip() and r.description_html:
                continue
            todo.append(r)
        log.info("Карточек: кеш {}, к загрузке {} из {}", reused, len(todo), len(records))
        done = 0
        batch = CONCURRENCY * HH_ENRICH_BATCH_MULT
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            await asyncio.gather(*(self._enrich(r) for r in chunk))
            done += len(chunk)
            if done % CFG.enrich_log_every < batch:
                log.info("  карточек обработано: {}/{}", done, len(todo))

    async def _collect_city(self, query: str, area_id: str, city: str) -> list[VacancyRecord]:
        """Все страницы одного города по одному запросу (страницы — последовательно)."""
        out: list[VacancyRecord] = []
        for page in range(MAX_PAGES):
            try:
                raw = await self._search_page(area_id, page, query)
            except Exception as e:
                log.warning("[{}] \"{}\" стр.{} ошибка: {}", city, query, page, e)
                break
            if not raw:
                break
            for item in raw:
                out.append(_record_from_search_item(item, city=city, city_id=area_id))
            if len(raw) < PER_PAGE:
                break
        log.info("[{}] \"{}\": {}", city, query, len(out))
        return out

    async def collect_all(self) -> list[VacancyRecord]:
        # Этап 1: списки из поиска — города собираются параллельно
        # (семафор CONCURRENCY + ротация прокси разруливают одновременные запросы).
        log.info("Этап 1: сбор списков, {} запросов × {} городов параллельно...",
                 len(SEARCH_QUERIES), len(CITIES))
        tasks = [self._collect_city(q, area_id, city)
                 for q in SEARCH_QUERIES
                 for area_id, city in CITIES.items()]
        results = await asyncio.gather(*tasks)

        # Дедуп по id (после параллельного сбора — без гонок)
        seen: set[str] = set()
        all_items: list[VacancyRecord] = []
        for lst in results:
            for rec in lst:
                if rec.vacancy.id in seen:
                    continue
                seen.add(rec.vacancy.id)
                all_items.append(rec)
        log.info("Уникальных вакансий: {}", len(all_items))

        # Отсев заведомо не-IT по ТАЙТЛУ (стройка/ритейл/транспорт/КИПиА) ДО enrich —
        # экономит самый долгий этап (карточки). По анализу такой блеклист не режет
        # реальный IT (тех-сигнал у IT-вакансий в сниппете, а не в этих тайтлах).
        before = len(all_items)
        all_items = [r for r in all_items if not is_hard_non_it(r.vacancy.name)]
        log.info("Отсеяно не-IT по блеклисту: {} (карточек к загрузке: {})",
                 before - len(all_items), len(all_items))

        # Этап 2: карточки (описание + навыки + HTML-описание)
        log.info("Этап 2: загрузка {} карточек...", len(all_items))
        await self.run_enrich(all_items)
        return all_items
