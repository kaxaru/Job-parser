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

from hrwork.config import (
    HIRIFY_ENRICH_CONCURRENCY,
    HIRIFY_ENRICH_MAX,
    HIRIFY_HOURLY_MAX_USD,
    HIRIFY_PAGE_CONCURRENCY,
    HIRIFY_PARAMS,
    HIRIFY_YEARLY_MIN_USD,
    MONTHS_PER_YEAR,
    WORK_HOURS_PER_MONTH,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
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


CFG = HirifyCfg()


def _infer_period(usd_mid) -> str:
    """Период зарплаты по USD-величине (в JSON поля нет). junior/middle: <$300 -> час,
    >$25k -> год, иначе месяц. Приблизительно (спорный $15–25k -> месяц)."""
    v = usd_mid or 0
    if 0 < v < HIRIFY_HOURLY_MAX_USD:
        return "hour"
    if v > HIRIFY_YEARLY_MIN_USD:
        return "year"
    return "month"


def _to_monthly(amount, period: str):
    """Сумму в ИСХОДНОЙ валюте -> месячная: час×160 (раб.часов/мес), год/12, месяц как есть."""
    if amount is None:
        return None
    if period == "hour":
        return round(amount * WORK_HOURS_PER_MONTH)
    if period == "year":
        return round(amount / MONTHS_PER_YEAR)
    return amount


def _clean_company(title) -> str:
    """company_title у hirify бывает плейсхолдером ('%hirify_global%') или None — в пусто."""
    t = (title or "").strip()
    return "" if (not t or t.startswith("%")) else t


def _meta_header(it: dict) -> str:
    """Мета-шапка для модалки: страны-наниматели · уровень English · норм. зарплата (USD).
    hirify-специфика, которой нет у HH — выносим явно поверх описания.

    ОСОЗНАННЫЙ компромисс слоёв: адаптер-инфраструктура генерит презентационный HTML.
    Чистый вариант (шапку строит feed) потребовал бы персистить regions/english_level/
    salary_in_usd отдельными полями raw-схемы — description_html собирается ЗДЕСЬ, на этапе
    collect, и уходит на диск целиком. Схему не раздуваем ради одной шапки; сложность
    инкапсулирована в одном месте. Санитайзер feed (allowlist) шапку пропускает (<p>+текст)."""
    regions = it.get("regions") or []
    countries = ", ".join(r.get("name_en", "") for r in regions if r.get("name_en")) or "Remote"
    parts = [f"🌍 {countries}"]
    eng = (it.get("english_level") or "").upper()
    if eng:
        parts.append(f"🗣 English {eng}")
    s = it.get("salary") or {}
    if s.get("salary_in_usd"):
        parts.append(f"💰 ~${s['salary_in_usd']:,} {s.get('currency', '')} (норм.)")
    parts.append("📌 hirify.me")
    return "<p>" + html.escape(" · ".join(parts)) + "</p>"


def _sig(it: dict) -> str:
    """Маркер изменения вакансии для инкрементального enrich (updated_at, иначе created_at)."""
    return it.get("updated_at") or it.get("created_at") or ""


def _normalize(it: dict, full: dict | None = None, *,
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
    if s and (s.get("min") is not None or s.get("max") is not None):
        period = _infer_period(s.get("salary_in_usd"))
        salary = Salary(_to_monthly(s.get("min"), period), _to_monthly(s.get("max"), period),
                        s.get("currency"), gross=False)     # hirify отдаёт net
    else:
        salary = None
    regions = it.get("regions") or []
    tags  = [t.get("name", "") for t in (it.get("tags") or [])]
    specs = [sp.get("name_en", "") for sp in (it.get("specializations") or [])]
    # теги/спец в detect_text -> детект стека/роли срабатывает штатно
    snippet = " ".join(filter(None, [it.get("tldr") or "", *tags, *specs]))
    # описание: из кеша (без сети) | полный текст /slug | tldr-заглушка (дозагрузим позже)
    if cached_desc is not None:
        desc_html, enriched = cached_desc, True
        at = enriched_at or storage.now_iso()           # переносим метку; None (legacy) -> заводим часы
    else:
        full_text = (full or {}).get("text")
        desc_html = _meta_header(it) + (full_text or it.get("tldr") or "")
        enriched = bool(full_text)
        at = storage.now_iso() if enriched else None    # реальная дозагрузка -> метка времени
    name = it.get("title", "")
    vac = build_vacancy(
        vid=f"hirify_{it.get('id')}",           # неймспейс — не сталкивается с числовыми id HH
        name=name,
        city=(regions[0].get("name_en") if regions else None) or "Remote",
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

    def __init__(self, **_):
        pass                                    # прокси/сессия не нужны — публичный API

    async def _curl_json(self, url: str) -> dict | None:
        """GET url -> распарсенный JSON или None после ретраев с бэкоффом."""
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    return json.loads(out.decode("utf-8", "replace"))
                except json.JSONDecodeError as e:
                    log.debug("hirify {}: {}", url, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return None

    async def _get_page(self, page: int) -> dict | None:
        return await self._curl_json(f"{CFG.api_url}?{HIRIFY_PARAMS}&page={page}")

    async def _get_one(self, slug: str) -> dict | None:
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
        items: list[dict] = list(first.get("data") or [])
        # пагинация Laravel: last_page/total на верхнем уровне ответа (meta нет)
        last_page = min(int(first.get("last_page") or 1), CFG.max_pages)
        total = int(first.get("total") or len(items))
        log.info("hirify: total={} last_page={} — тяну список…", total, last_page)

        sem = asyncio.Semaphore(CFG.page_conc)

        async def _page(p: int) -> list[dict]:
            async with sem:
                d = await self._get_page(p)
            return (d or {}).get("data") or []

        if last_page > 1:
            pages = await asyncio.gather(*(_page(p) for p in range(2, last_page + 1)))
            for chunk in pages:
                items.extend(chunk)
        log.info("hirify: список собран — {} вакансий", len(items))

        # Этап 2: инкрементальный enrich. Описания из прошлого сбора берём из кеша (сеть не трогаем),
        # /slug тянем ТОЛЬКО для новых/изменившихся (по _sig), не более HIRIFY_ENRICH_MAX за прогон.
        # Так полное покрытие описаниями растёт день за днём без пере-скачивания неизменных.
        cache = storage.load_desc_cache()               # {id: {sig, description_html, requirement, at}}
        reuse: list[tuple[dict, dict]] = []             # (item, hit) — sig совпал и не протух
        todo:  list[dict] = []                          # новые/изменившиеся/протухшие — кандидаты на /slug
        for it in items:
            hit = cache.get(f"hirify_{it.get('id')}")
            if storage.cache_hit_usable(hit, _sig(it)):
                reuse.append((it, hit))
            else:
                todo.append(it)
        todo.sort(key=lambda it: it.get("created_at") or "", reverse=True)   # свежие — в приоритет
        to_enrich, tldr_only = todo[:CFG.enrich_max], todo[CFG.enrich_max:]

        esem = asyncio.Semaphore(CFG.enrich_conc)

        async def _enrich(it: dict) -> VacancyRecord:
            async with esem:
                full = await self._get_one(it.get("slug", ""))
            return _normalize(it, full)

        enriched = list(await asyncio.gather(*(_enrich(it) for it in to_enrich))) if to_enrich else []
        out = [_normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))
               for it, hit in reuse]
        out += enriched
        out += [_normalize(it) for it in tldr_only]     # без полного описания — доберём в следующий прогон
        log.info("hirify: собрано {} (кеш {}, дозагружено {}, tldr {})",
                 len(out), len(reuse), len(enriched), len(tldr_only))
        return out
