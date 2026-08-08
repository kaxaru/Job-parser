"""Источник hirify.me — JSON-API агрегатора (api.hirify.me/api/vacancies).

В отличие от HH (HTML + DDoS-Guard + 2 стадии), hirify отдаёт все поля в списке постранично,
поэтому сбор — чистая пагинация API (без stage-2 enrich). Нормализуем в каноническую
raw-схему (как html_client._normalize_search_item), чтобы parse_vacancy/Analyzer/views
работали без изменений. Отклики (Playwright) к hirify неприменимы — карточки открываются
прямой ссылкой (alternate_url).
"""
import asyncio
import html
import json
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    HIRIFY_ENRICH_CONCURRENCY,
    HIRIFY_ENRICH_MAX,
    HIRIFY_PAGE_CONCURRENCY,
    HIRIFY_PARAMS,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary, SalaryPeriod
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, check_list_complete, normalize_each, register_source
from .hh import BROWSER_UA


# ── Конфиг сбора hirify (транспорт + конкурентность; env-ручки берутся из config) ──
@dataclass(frozen=True)
class HirifyCfg:
    api_url: str = "https://api.hirify.me/api/vacancies"
    max_pages: int = 2000                          # страховка от кривого last_page (~1200 стр.)
    retry_attempts: int = 4                        # публичный JSON-API легче HH -> меньше попыток
    backoff_start: float = 1.0
    backoff_max: float = 10.0                      # ниже потолок паузы, чем у HH
    page_conc: int = HIRIFY_PAGE_CONCURRENCY       # параллельных страниц списка (env)
    enrich_conc: int = HIRIFY_ENRICH_CONCURRENCY   # параллельных /slug (env)
    enrich_max: int = HIRIFY_ENRICH_MAX            # порог полного enrich vs tldr-заглушка (env)
    # Доля списка, которую можно недобрать молча (сверку делает base.check_list_complete).
    # ПОРОГ 2 % выбран так:
    #   * одна страница hirify — 100 записей из ~18 000, то есть 0.55 %. Ронять весь прогон
    #     из-за одного транзиентного сбоя нельзя: записи вернутся следующим сбором, а их
    #     описания уложатся в дневной бюджет `HIRIFY_ENRICH_MAX = 600` (2 % от 18k = ~360);
    #   * выше 2 % усечённый срез стоит дороже пропуска прогона: восстановление описаний
    #     растянется на несколько дней бюджета, а кеш уже затёрт.
    list_loss_max_ratio: float = 0.02


CFG = HirifyCfg()


def _usd_mid(s: dict[str, Any] | None) -> float | None:
    """USD-серединка вилки — вход для инференса периода (`SalaryPeriod.infer`).
    Периода в схеме hirify нет вовсе, поэтому его определяют по величине."""
    raw = (s or {}).get("salary_in_usd")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _clean_company(title: Any) -> str:
    """company_title у hirify бывает плейсхолдером ('%hirify_global%') или None — в пусто."""
    t = (title or "").strip()
    return "" if (not t or t.startswith("%")) else t


def _meta_header(it: dict[str, Any]) -> str:
    """Мета-шапка для модалки: страны-наниматели · уровень English · норм. зарплата (USD).
    hirify-специфика, которой нет у HH — выносим явно поверх описания.

    ОСОЗНАННЫЙ компромисс слоёв: адаптер-инфраструктура генерит презентационный HTML.
    Чистый вариант (шапку строит feed) потребовал бы персистить regions/english_level/
    salary_in_usd отдельными полями raw-схемы — description_html собирается ЗДЕСЬ, на этапе
    collect, и уходит на диск целиком. Схему не раздуваем ради одной шапки; сложность
    инкапсулирована в одном месте. Санитайзер feed (allowlist) шапку пропускает (<p>+текст)."""
    regions = it.get("regions") or []
    countries = ", ".join(r.get("name_en", "") for r in regions if r.get("name_en")) or REMOTE_CITY
    parts = [f"🌍 {countries}"]
    eng = (it.get("english_level") or "").upper()
    if eng:
        parts.append(f"🗣 English {eng}")
    s = it.get("salary") or {}
    # Гейт по ТИПУ, а не по truthy: `salary_in_usd` приходит и строкой ("120000"), а
    # спецификатор `:,` на строке даёт ValueError («Cannot specify ',' with 's'») — одна
    # такая карточка роняла нормализацию ВСЕЙ страницы. `_usd_mid` — мягкий парсер внешних
    # данных: строка/мусор/None -> None. Ноль отбрасываем: у портала это «нет данных».
    usd = _usd_mid(s)
    if usd is not None and usd > 0:
        parts.append(f"💰 ~${usd:,.0f} {s.get('currency', '')} (норм.)")
    parts.append("📌 hirify.me")
    return "<p>" + html.escape(" · ".join(parts)) + "</p>"


def _sig(it: dict[str, Any]) -> str:
    """Маркер изменения вакансии для инкрементального enrich (updated_at, иначе created_at)."""
    return it.get("updated_at") or it.get("created_at") or ""


def _normalize(it: dict[str, Any], full: dict[str, Any] | None = None, *,
               cached_desc: str | None = None, enriched_at: str | None = None) -> VacancyRecord:
    """Элемент hirify-API -> VacancyRecord (ACL: hirify JSON СРАЗУ в домен, без HH-схемы).
    full — ответ /api/vacancies/{slug} с полным `text`; cached_desc — готовое описание из кеша
    прошлого сбора (тогда сеть не трогаем), enriched_at — исходная метка дозагрузки (переносим,
    чтобы max-age считался от реального фетча). enriched=True только при полном описании.
    В отличие от HH, techs у hirify есть сразу (tldr+теги+спец), enrich добирает лишь описание."""
    s = it.get("salary")
    # период не трекается -> инферим по USD-величине и приводим вилку к МЕСЯЦУ (в исходной
    # валюте); дальше JS конвертит валюту. Так HH(RUR/мес) и hirify сравнимы на одной оси.
    # Контракт «нет вилки -> None» (как Salary.from_raw): словарь с одной валютой без
    # min/max не должен рождать truthy Salary(None, None, …)-шелуху.
    # Пересчёт и порог инференса — в домене (Salary.monthly / SalaryPeriod.infer): период
    # определяется одинаково для hirify, himalayas и web3.career. Раньше правило жило здесь,
    # и новые адаптеры завели свои копии с разными порогами (07.08.2026).
    salary = Salary.monthly(
        (s or {}).get("min"), (s or {}).get("max"), (s or {}).get("currency"),
        SalaryPeriod.infer(_usd_mid(s)),
        gross=False,                                        # hirify отдаёт net
    ) if s else None
    regions = it.get("regions") or []
    tags  = [t.get("name", "") for t in (it.get("tags") or [])]
    specs = [sp.get("name_en", "") for sp in (it.get("specializations") or [])]
    # теги/спец в detect_text -> детект стека/роли срабатывает штатно
    snippet = " ".join(filter(None, [it.get("tldr") or "", *tags, *specs]))
    # описание: из кеша (без сети) | полный текст /slug | tldr-заглушка (дозагрузим позже)
    if cached_desc is not None:
        desc_html, enriched = cached_desc, True
        at: str | None = enriched_at or storage.now_iso()  # переносим метку; None (legacy) -> заводим часы
    else:
        full_text = (full or {}).get("text")
        desc_html = _meta_header(it) + (full_text or it.get("tldr") or "")
        enriched = bool(full_text)
        at = storage.now_iso() if enriched else None    # реальная дозагрузка -> метка времени
    name = it.get("title", "")
    vac = build_vacancy(
        vid=f"hirify_{it.get('id')}",           # неймспейс — не сталкивается с числовыми id HH
        name=name,
        city=(regions[0].get("name_en") if regions else None) or REMOTE_CITY,
        city_id="",
        salary=salary,
        experience=Experience.from_hirify_grades(it.get("grades")),   # VO напрямую, без HH-кода
        schedule=Schedule.from_hirify_wf(it.get("work_format")),
        detect_text=name + " " + snippet,
        employer=_clean_company(it.get("company_title")),
        created_at=it.get("created_at"),
        published_at=it.get("created_at"),      # у hirify нет переоткрытия
        responses=None,
        source="hirify",
    )
    return VacancyRecord(vacancy=vac, url=f"https://hirify.me/jobs/{it.get('slug', '')}",
                         description_html=desc_html, requirement=snippet,
                         sig=_sig(it), enriched=enriched, enriched_at=at)


@register_source("hirify")
class HirifySource(Source):
    """Сбор вакансий hirify через постраничный JSON-API."""

    name = "hirify"

    def __init__(self, **_: Any) -> None:
        pass                                    # прокси/сессия не нужны — публичный API

    async def _curl_json(self, url: str) -> dict[str, Any] | None:
        """GET url -> распарсенный JSON или None после ретраев с бэкоффом."""
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace"))
                    return payload
                except json.JSONDecodeError as e:
                    log.debug("hirify {}: {}", url, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return None

    async def _get_page(self, page: int) -> dict[str, Any] | None:
        return await self._curl_json(f"{CFG.api_url}?{HIRIFY_PARAMS}&page={page}")

    async def _get_one(self, slug: str) -> dict[str, Any] | None:
        """Одиночная вакансия /api/vacancies/{slug} — полный `text` (описание)."""
        if not slug:
            return None
        d = await self._curl_json(f"{CFG.api_url}/{slug}")
        return d.get("data", d) if isinstance(d, dict) else None

    async def collect(self) -> list[VacancyRecord]:
        # Этап 1: страница 1 -> last_page, дальше 2..N параллельно (HIRIFY_PAGE_CONCURRENCY).
        first = await self._get_page(1)
        if not first:
            log.warning("hirify: страница 1 пуста — источник недоступен")
            return []
        items: list[dict[str, Any]] = list(first.get("data") or [])
        # пагинация Laravel: last_page/total на верхнем уровне ответа (meta нет)
        last_page = min(int(first.get("last_page") or 1), CFG.max_pages)
        total = int(first.get("total") or len(items))
        per_page = int(first.get("per_page") or len(items) or 1)
        log.info("hirify: total={} last_page={} — тяну список…", total, last_page)

        sem = asyncio.Semaphore(CFG.page_conc)
        failed: list[int] = []                          # страницы, не отдавшиеся после всех ретраев

        async def _page(p: int) -> list[dict[str, Any]]:
            async with sem:
                d = await self._get_page(p)
            if d is None:            # сбой транспорта — это НЕ «страница честно пустая»
                failed.append(p)
                return []
            data: list[dict[str, Any]] = d.get("data") or []
            return data

        if last_page > 1:
            pages = await asyncio.gather(*(_page(p) for p in range(2, last_page + 1)))
            for chunk in pages:
                items.extend(chunk)
        log.info("hirify: список собран — {} вакансий", len(items))
        check_list_complete(len(items), total=total, per_page=per_page, failed=failed,
                            source="hirify", max_lost_ratio=CFG.list_loss_max_ratio)

        # Этап 2: инкрементальный enrich. Описания из прошлого сбора берём из кеша (сеть не трогаем),
        # /slug тянем ТОЛЬКО для новых/изменившихся (по _sig), не более HIRIFY_ENRICH_MAX за прогон.
        # Так полное покрытие описаниями растёт день за днём без пере-скачивания неизменных.
        cache = storage.load_desc_cache()               # схема записи — storage/files.py::load_desc_cache
        reuse: list[tuple[dict[str, Any], dict[str, Any]]] = []   # (item, hit): sig совпал и не протух
        todo:  list[dict[str, Any]] = []                          # новые/изменившиеся/протухшие — кандидаты на /slug
        stale: dict[str, dict[str, Any]] = {}                     # та же версия, но описание протухло
        for it in items:
            vid, sig = f"hirify_{it.get('id')}", _sig(it)
            hit = cache.get(vid)
            if storage.cache_hit_usable(hit, sig):
                reuse.append((it, hit))
                continue
            todo.append(it)
            if storage.cache_hit_matches(hit, sig):
                stale[vid] = hit
        todo.sort(key=lambda it: it.get("created_at") or "", reverse=True)   # свежие — в приоритет
        # Бюджет /slug — СНАЧАЛА тем, у кого описания нет вовсе. У протухших оно есть и
        # переживает прогон (fallback ниже), а у никогда-не-обогащённых альтернатива — tldr,
        # и раньше именно они не доходили до enrich никогда: экспирация съедала весь бюджет.
        # Сортировка стабильная, поэтому порядок «свежие раньше» внутри групп сохраняется.
        todo.sort(key=lambda it: f"hirify_{it.get('id')}" in stale)
        to_enrich, tail = todo[:CFG.enrich_max], todo[CFG.enrich_max:]

        esem = asyncio.Semaphore(CFG.enrich_conc)

        async def _fetch_full(it: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            async with esem:
                full = await self._get_one(it.get("slug", ""))
            return it, full

        def _from_cache(pair: tuple[dict[str, Any], dict[str, Any]]) -> VacancyRecord:
            it, hit = pair
            return _normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))

        def _from_full(pair: tuple[dict[str, Any], dict[str, Any] | None]) -> VacancyRecord:
            return _normalize(pair[0], pair[1])

        def _from_tail(it: dict[str, Any]) -> VacancyRecord:
            hit = stale.get(f"hirify_{it.get('id')}")
            if hit is None:
                return _normalize(it)               # описания не было — tldr, доберём следующим прогоном
            # Бюджет кончился, а описание в кеше есть: ПРОТУХШЕЕ ЛУЧШЕ tldr-шапки. Метку `at`
            # переносим как есть — запись остаётся кандидатом на обновление, но уже скачанный
            # текст больше не стирается (раньше хвост сверх бюджета откатывался на tldr).
            return _normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))

        fetched = list(await asyncio.gather(*(_fetch_full(it) for it in to_enrich))) if to_enrich else []
        # Нормализация — через normalize_each: кривая карточка портала не должна ронять
        # источник целиком (иначе санити-гейт видит нулевой срез и морозит кеш всех порталов).
        out = normalize_each(reuse, _from_cache, source="hirify")
        enriched = normalize_each(fetched, _from_full, source="hirify")
        out += enriched
        kept_stale = sum(1 for it in tail if f"hirify_{it.get('id')}" in stale)
        out += normalize_each(tail, _from_tail, source="hirify")
        log.info("hirify: собрано {} (кеш {}, дозагружено {}, протухший кеш {}, tldr {})",
                 len(out), len(reuse), len(enriched), kept_stale, len(tail) - kept_stale)
        return out
