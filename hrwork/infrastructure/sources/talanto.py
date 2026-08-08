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
from typing import Any

from hrwork.config import (
    TALANTO_ENRICH_CONCURRENCY,
    TALANTO_ENRICH_MAX,
    TALANTO_PAGE_CONCURRENCY,
    TALANTO_PARAMS,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy, is_hard_non_it
from hrwork.domain.salary import Salary, SalaryPeriod
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, check_list_complete, normalize_each, register_source
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
    # Доля списка, которую можно недобрать молча (сверку делает base.check_list_complete).
    # ПОРОГ 2 % — тот же, что у hirify, и по той же причине: порталы одного размера и формы
    # (~40k против ~18k записей, страница 100).
    #   * одна страница talanto — 100 записей из ~40 000, то есть 0.25 %. Ронять весь прогон
    #     (пустой срез -> санити-гейт не сохраняет кеш ВООБЩЕ) из-за одного транзиентного
    #     сбоя нельзя: записи вернутся следующим сбором;
    #   * выше 2 % (~800 записей) восстановление описаний перестаёт укладываться в дневной
    #     бюджет `TALANTO_ENRICH_MAX = 600`, и усечённый срез стоит дороже пропуска прогона.
    list_loss_max_ratio: float = 0.02


CFG = TalantoCfg()


# Подписи локации talanto, означающие «места нет», а не город. Это СЛОВАРЬ ПОРТАЛА (ACL):
# какими словами конкретный портал называет отсутствие привязки — знание о портале, а во что
# они превращаются — доменная константа REMOTE_CITY, одна на все источники.
# Замер по кешу 08.08.2026: «Anywhere in the World» 326 записей, «Worldwide» 8, «World» 5,
# «Удалённо»/«Удаленно» 3 лежали отдельными бакетами рядом с «Remote» (9 286) — ровно тот
# дефект, ради которого REMOTE_CITY и заводилась (инцидент 07.08.2026: 11 978 против 219).
# Уточнённые формы («Remote - Europe», «Удалённо по РФ») НЕ сводятся: там есть география,
# и терять её нельзя — это не «места нет», а «место названо широко».
_NO_PLACE_LABELS = frozenset({
    "remote", "remote work", "worldwide", "world", "anywhere", "anywhere in the world",
    "удаленно", "удаленка", "удаленная работа",
})


def _clean_city(loc: str | None) -> str:
    """talanto.location бывает списком стран («Anywhere in the World, 🇦🇩 Andorra, …» до 2400
    симв.) вместо города — это мусор в измерении «город» (ломал верстку дашборда). Берём
    первый сегмент до запятой и режем длину; настоящий город («Москва (м. Киевская)») цел.
    Сегмент, означающий «места нет», сводится к доменной REMOTE_CITY (`_NO_PLACE_LABELS`)."""
    head = " ".join((loc or "").split(",")[0].split())
    if head.lower().replace("ё", "е") in _NO_PLACE_LABELS:
        return REMOTE_CITY
    return head[:80]

# Таблицы расписаний здесь БОЛЬШЕ НЕТ: `_SCHED = {"remote": …, "hybrid": …, "office": …}`
# переехала в домен (`Schedule.from_talanto`, мягкий контракт: неизвестное -> None, дефолт
# OFFICE ставит вызывающий). Причина ровно та же, что была с грейдами ниже: адаптерная копия
# доменной таблицы тихо расходится с оригиналом, и один и тот же код портала начинает значить
# на разных срезах разное (аудит 08.08.2026).
# Таблицы грейдов здесь НЕТ — её ведёт домен (Experience.from_grades). Своя копия
# РАСХОДИЛАСЬ с ним: «junior» она клала в «Без опыта», тогда как hirify/getmatch/himalayas/
# jobicy/themuse через домен дают «1–3 года» (найдено аудитом 07.08.2026). Один и тот же
# грейд обязан значить одно и то же на всех порталах — по нему идут и отбор под отклик,
# и срез salary_by_experience. Заодно точное равенство не понимало «Mid-level» и «Senior/Lead»,
# а вхождение подстроки в домене понимает.


def _title(it: dict[str, Any]) -> str:
    """Тайтл карточки строкой. `str()` не косметика: отсев `is_hard_non_it` идёт по СЫРОМУ
    полю ДО нормализации, вне изоляции `normalize_each`, и дрейф схемы (объект вместо строки)
    ронял бы там весь источник — а это [] от `hh.py::_run_source` и замороженный кеш ВСЕХ
    порталов. На чужих данных деградируем, а не падаем."""
    t = it.get("title")
    return str(t) if t else ""


def _sig(it: dict[str, Any]) -> str:
    """Маркер изменения для инкрементального enrich (last_verified_at, иначе published_at)."""
    return it.get("last_verified_at") or it.get("published_at") or ""


def _meta_header(it: dict[str, Any], full: dict[str, Any] | None) -> str:
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


def _normalize(it: dict[str, Any], full: dict[str, Any] | None = None, *,
               cached_desc: str | None = None, enriched_at: str | None = None) -> VacancyRecord:
    """Элемент talanto-API -> VacancyRecord (ACL). full — ответ /api/jobs/{id} с description;
    cached_desc — описание из кеша прошлого сбора (сеть не трогаем)."""
    # Вилка в домен идёт ТОЛЬКО через `Salary.monthly` — единственный вход в месячную ось,
    # и период называется ЯВНО. Контракт «нет вилки -> None» держит сама фабрика.
    # gross неизвестен (в текстах встречается «gross», но поля нет) — оставляем как есть
    # (gross=False, НДФЛ не вычитаем повторно; лучше показать заявленное, чем занизить).
    #
    # ПЕРИОД ЗДЕСЬ MONTH, А НЕ `SalaryPeriod.infer` — и это проверено, а не предположено.
    # Замер по кешу 08.08.2026 (109 595 записей) говорит, что talanto отдаёт УЖЕ месячные
    # суммы, деля годовые на 12 на своей стороне:
    #   * 35.9 % USD-границ talanto имеют «/12-подпись» (16 833 = 202 000/12, 5 833 =
    #     70 000/12, 2 083 = 25 000/12). У hirify, где вилка приведена к месяцу нами, та же
    #     доля 52.7 %, а у hh (человеческие месячные) — 0.0 %;
    #   * медианы сходятся с месячными: USD 10 000 против 11 825 у hirify, RUR 110 000
    #     против 100 000 у hh. Годовые дали бы порядок 120 000 USD.
    # Инференс на этих данных не «уточнил» бы период, а СЛОМАЛ вилку: 6 415 из 6 442
    # RUR-границ больше порога 25 000 -> «год» -> делёж на 12 (у talanto нет поля
    # `salary_in_usd`, по которому hirify считает величину для инференса), а USD 200/217/243
    # меньше 300 -> «час» -> ×160.
    salary = Salary.monthly(it.get("salary_min"), it.get("salary_max"),
                            it.get("salary_currency"), SalaryPeriod.MONTH, gross=False)
    skills = [s for s in (it.get("skills") or []) if s]
    snippet = " ".join(skills)
    if cached_desc is not None:
        desc_html, enriched = cached_desc, True
        at: str | None = enriched_at or storage.now_iso()
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
        experience=Experience.from_grades([it.get("level")]),
        # мягкий доменный парсер: пусто/незнакомое -> None, дефолт OFFICE ставим здесь
        schedule=Schedule.from_talanto(it.get("remote_type")) or Schedule.OFFICE,
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

    def __init__(self, **_: Any) -> None:
        pass                                    # публичный API — прокси/сессия не нужны

    async def _curl_json(self, url: str) -> dict[str, Any] | None:
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "ru"}
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    payload: dict[str, Any] = json.loads(out.decode("utf-8", "replace"))
                    return payload
                except json.JSONDecodeError as e:
                    log.debug("talanto {}: {}", url, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return None

    async def _get_page(self, offset: int) -> dict[str, Any] | None:
        return await self._curl_json(f"{CFG.api_url}?{TALANTO_PARAMS}&offset={offset}")

    async def _get_one(self, vid: str) -> dict[str, Any] | None:
        """Карточка /api/jobs/{id} — полное HTML-описание + url первоисточника."""
        return await self._curl_json(f"{CFG.api_url}{vid}") if vid else None

    async def collect(self) -> list[VacancyRecord]:
        # Этап 1: offset=0 -> total, дальше страницы параллельно (limit/offset, не page).
        first = await self._get_page(0)
        if not first:
            log.warning("talanto: страница 0 пуста — источник недоступен")
            return []
        items: list[dict[str, Any]] = list(first.get("items") or [])
        total = int(first.get("total") or len(items))
        pages = min((total + CFG.page_size - 1) // CFG.page_size, CFG.max_pages)
        log.info("talanto: total={} страниц={} — тяну список…", total, pages)

        sem = asyncio.Semaphore(CFG.page_conc)
        failed: list[int] = []                  # offset'ы, не отдавшиеся после всех ретраев

        async def _page(offset: int) -> list[dict[str, Any]]:
            async with sem:
                d = await self._get_page(offset)
            if d is None:                       # сбой транспорта — это НЕ «страница честно пуста»
                failed.append(offset)
                return []
            chunk: list[dict[str, Any]] = d.get("items") or []
            return chunk

        if pages > 1:
            chunks = await asyncio.gather(*(_page(p * CFG.page_size) for p in range(1, pages)))
            for chunk in chunks:
                items.extend(chunk)
        log.info("talanto: список собран — {} вакансий", len(items))
        if pages >= CFG.max_pages and total > CFG.max_pages * CFG.page_size:
            # Усечение обхода не молчит — то же правило, что в himalayas::collect.
            log.warning("talanto: обход УПЁРСЯ В ЛИМИТ {} страниц (total={}) — часть вакансий "
                        "не собрана", CFG.max_pages, total)
        check_list_complete(len(items), total=min(total, pages * CFG.page_size),
                            per_page=CFG.page_size, failed=failed,
                            source="talanto", max_lost_ratio=CFG.list_loss_max_ratio,
                            failed_kind="offset'ы")

        # Этап 2: инкрементальный enrich описаний (тот же паттерн, что hirify): кеш по _sig,
        # /jobs/{id} только для новых/изменившихся, не-IT тайтлы не обогащаем вовсе
        # (is_hard_non_it ДО enrich — как у HH: не качаем карточки заведомо чужих).
        cache = storage.load_desc_cache()
        reuse: list[tuple[dict[str, Any], dict[str, Any]]] = []   # sig совпал и не протух
        todo: list[dict[str, Any]] = []                           # кандидаты на /jobs/{id}
        skipped: list[dict[str, Any]] = []                        # не-IT: карточку не тянем
        stale: dict[str, dict[str, Any]] = {}                     # та же версия, но описание протухло
        for it in items:
            vid, sig = f"talanto_{it.get('id')}", _sig(it)
            hit = cache.get(vid)
            if storage.cache_hit_usable(hit, sig):
                reuse.append((it, hit))
                continue
            if storage.cache_hit_matches(hit, sig):
                stale[vid] = hit
            # не-IT тайтл идёт мимо enrich, но НЕ мимо выдачи: раньше такая запись, уже
            # лежащая в кеше с протухшим описанием, не попадала ни в reuse, ни в todo,
            # ни в хвост — и молча исчезала из среза.
            (skipped if is_hard_non_it(_title(it)) else todo).append(it)
        todo.sort(key=lambda it: it.get("published_at") or "", reverse=True)   # свежие — в приоритет
        # Бюджет /jobs/{id} — СНАЧАЛА тем, у кого описания нет вовсе (та же схема, что в
        # `hirify.py::collect`). У протухших оно есть и переживает прогон (fallback ниже),
        # а у никогда-не-обогащённых альтернатива — пустое описание. Иначе экспирация съедает
        # весь бюджет: ~40k вакансий при `TALANTO_ENRICH_MAX = 600` и жизни записи 14 дней
        # дают потолок покрытия 8 400 = 21 %. Сортировка стабильная, порядок «свежие раньше»
        # внутри групп сохраняется.
        todo.sort(key=lambda it: f"talanto_{it.get('id')}" in stale)
        to_enrich, tail = todo[:CFG.enrich_max], todo[CFG.enrich_max:]

        esem = asyncio.Semaphore(CFG.enrich_conc)

        async def _fetch_full(it: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            async with esem:
                full = await self._get_one(it.get("id", ""))
            return it, full

        def _from_cache(pair: tuple[dict[str, Any], dict[str, Any]]) -> VacancyRecord:
            it, hit = pair
            return _normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))

        def _from_full(pair: tuple[dict[str, Any], dict[str, Any] | None]) -> VacancyRecord:
            return _normalize(pair[0], pair[1])

        def _from_tail(it: dict[str, Any]) -> VacancyRecord:
            hit = stale.get(f"talanto_{it.get('id')}")
            if hit is None:
                return _normalize(it)           # описания не было — доберём следующим прогоном
            # Карточку не тянем (бюджет кончился или тайтл не-IT), а описание в кеше есть:
            # ПРОТУХШЕЕ ЛУЧШЕ ПУСТОГО. Метку `at` переносим как есть — запись остаётся
            # кандидатом на обновление, но уже скачанный текст больше не стирается.
            return _normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"))

        fetched = list(await asyncio.gather(*(_fetch_full(it) for it in to_enrich))) if to_enrich else []
        # Нормализация — через normalize_each: кривая карточка портала не должна ронять
        # источник целиком (иначе санити-гейт видит нулевой срез и морозит кеш всех порталов).
        out = normalize_each(reuse, _from_cache, source="talanto")
        enriched = normalize_each(fetched, _from_full, source="talanto")
        out += enriched
        rest = tail + skipped
        kept_stale = sum(1 for it in rest if f"talanto_{it.get('id')}" in stale)
        out += normalize_each(rest, _from_tail, source="talanto")
        log.info("talanto: собрано {} (кеш {}, дозагружено {}, протухший кеш {}, без описания {})",
                 len(out), len(reuse), len(enriched), kept_stale, len(rest) - kept_stale)
        return out
