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

from .base import Source, check_list_complete, normalize_each, register_source
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
    # Доля списка, которую можно недобрать молча (сверку делает base.check_list_complete).
    # ПОРОГ 25 % намеренно ГРУБЕЕ, чем 2 % у hirify/talanto: getmatch маленький (~740
    # активных вакансий), и одна страница — это сразу 13.5 % портала. Ронять из-за неё весь
    # прогон нельзя: `ListIncomplete` -> пустой срез -> санити-гейт НЕ сохраняет кеш ВООБЩЕ,
    # то есть одна сбойная страница портала на 0.7 % корпуса выбросила бы многочасовой сбор
    # остальных восьми. Одна страница -> WARNING и берём что есть (записи вернутся следующим
    # прогоном); две и больше (>= 27 %) — это уже не транзиентный сбой, а недоступность
    # портала, и усечённый срез хуже пропуска.
    list_loss_max_ratio: float = 0.25


CFG = GetmatchCfg()

# Промо-блок «one day offer» приезжает в той же выдаче, но это не вакансия, а анонс
# однодневного мероприятия: у него нет ни грейда, ни зарплаты, ни нормального описания.
_OFFER_TYPE_VACANCY = "vacancy"

# Подписи локации getmatch, означающие «места нет», а не город. Это СЛОВАРЬ ПОРТАЛА (ACL):
# какими словами конкретный портал называет отсутствие привязки — знание о портале, а во что
# они превращаются — доменная константа REMOTE_CITY, одна на все источники.
# Замер по кешу 08.08.2026: «Весь мир» 43 записи и «Worldwide» 12 лежали отдельными бакетами
# рядом с «Remote» от остальных порталов — ровно тот дефект, ради которого REMOTE_CITY
# и заводилась (инцидент 07.08.2026: 11 978 против 219).
_NO_PLACE_LABELS = frozenset({"весь мир", "worldwide"})


def _sig(it: dict[str, Any]) -> str:
    """Маркер изменения карточки: у getmatch правки публикации двигают published_at."""
    return str(it.get("published_at") or "")


def _city(it: dict[str, Any]) -> str:
    """Город из location_requirements (там он разобран), иначе подпись location_items.
    Пусто ЛИБО подпись «места нет» («Весь мир», «Worldwide») -> доменная REMOTE_CITY:
    одно состояние — один бакет в фасете городов ленты (см. `_NO_PLACE_LABELS`)."""
    raws = [str(req.get("city") or "") for req in (it.get("location_requirements") or [])]
    raws += [str(li.get("label") or "") for li in (it.get("location_items") or [])]
    for raw in raws:
        place = raw.strip()
        if place:
            return REMOTE_CITY if place.lower() in _NO_PLACE_LABELS else place
    return REMOTE_CITY


def _formats(it: dict[str, Any]) -> list[str]:
    return [str(li.get("format")) for li in (it.get("location_items") or []) if li.get("format")]


def _skills(it: dict[str, Any]) -> str:
    return " ".join(str(s.get("name")) for s in (it.get("skills_objects") or []) if s.get("name"))


def _salary(it: dict[str, Any]) -> Salary | None:
    """Вилка -> VO, УЖЕ приведённая к net. `salary_taxes` = gross|net приходит явно, поэтому
    НДФЛ считает Salary VO, а не мы. Скрытая вилка (salary_hidden у ~59 %) -> None,
    как контракт Salary.from_raw.

    `.net()` вызывается ЗДЕСЬ, ровно как в `hh.py::_record_from_search_item`, и это не
    косметика. getmatch — единственный источник, отдающий gross=True, а на диск запись
    ложится с `gross: false` (`repository.py::_to_dict`, «зарплата уже net»). Без вычета
    13 % gross-вилка «легализовалась» как net и после перезагрузки кеша была неотличима от
    честной: 200 000–300 000 gross давали медиану 250 000 вместо 217 500, завышение 14.9 %
    (аудит 08.08.2026)."""
    frm, to = it.get("salary_display_from"), it.get("salary_display_to")
    if frm is None and to is None:
        return None
    return Salary(frm, to, it.get("salary_currency"),
                  gross=(it.get("salary_taxes") == "gross")).net()


def _cached_experience(hit: dict[str, Any]) -> Experience | None:
    """Грейд из записи desc-кеша прошлого сбора (reuse-путь, сеть не трогаем).

    Зачем: грейда НЕТ в списке `/api/offers`, он приходит только карточкой. Пока кеш его не
    хранил, переиспользованная запись оставалась с experience=None — день 1 грейды были,
    а дальше ~740 карточек шли из кеша пустыми до 14-дневного протухания, то есть 13 дней
    из 14 срез по опыту для getmatch был пуст (аудит 08.08.2026).

    ЗАЩИЩЁННО: запись кеша СТАРОГО формата этого ключа не содержит -> вернём None, грейд
    просто останется пустым, как было, и адаптер не упадёт.

    Форма ровно ОДНА — сохранённый код доменного VO (`experience_id`). Он лежит в raw-схеме
    на диске с самого начала (`repository.py::_to_dict` пишет `experience.id`), а round-trip
    `Experience.hh_id -> Experience.from_code` возвращает тот же VO, поэтому грейд
    восстанавливается на уже существующем кеше, без пере-сбора. Сырые поля карточки
    (`seniority` / `required_years_of_experience`) в кеш НЕ кладутся намеренно: второй путь
    к тому же значению — это два примитива рядом с уже сохранённым VO, то есть источник
    расхождения (см. `storage/files.py::load_desc_cache`)."""
    return Experience.from_code(hit.get("experience_id"))


def _normalize(it: dict[str, Any], full: dict[str, Any] | None = None,
               cached_desc: str | None = None, enriched_at: str | None = None,
               cached_exp: Experience | None = None) -> VacancyRecord:
    """Карточка getmatch -> VacancyRecord (ACL: внешняя схема живёт только здесь).

    `full` — ответ /api/offers/{id}; `cached_desc` — описание из прошлого сбора (тогда сеть
    не трогаем), и вместе с ним `cached_exp` — грейд оттуда же (в списке его нет, см.
    `_cached_experience`). У необогащённых карточек, которых нет и в кеше, experience
    остаётся None — это честно, лучше пустого поля, чем выдуманный уровень."""
    src = full or it
    if cached_desc is not None:
        desc_html, enriched = cached_desc, True
        at: str | None = enriched_at or storage.now_iso()
        experience = cached_exp
    else:
        # description (полный HTML) есть только в карточке; в списке лежит короткая выжимка
        desc_html = str(src.get("description") or "") or str(it.get("offer_description") or "")
        enriched = bool(src.get("description"))
        at = storage.now_iso() if enriched else None
        # грейд + годы опыта — только из карточки (в списке полей нет)
        experience = Experience.from_getmatch(src.get("seniority"),
                                              src.get("required_years_of_experience"))

    name = str(it.get("position") or "")
    snippet = str(it.get("offer_description") or "")
    company = it.get("company") or {}
    published = it.get("published_at")
    vac = build_vacancy(
        vid=f"getmatch_{it.get('id')}",         # неймспейс — не сталкивается с id HH/hirify/talanto
        name=name,
        city=_city(it),
        city_id="",
        salary=_salary(it),                     # уже net (НДФЛ вычтен в _salary)
        experience=experience,
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
        failed: list[int] = []                  # offset'ы, не отдавшиеся после всех ретраев

        async def _page(offset: int) -> list[dict[str, Any]]:
            async with sem:
                d = await self._get_page(offset)
            if d is None:                       # сбой транспорта — это НЕ «страница честно пуста»
                failed.append(offset)
                return []
            offers: list[dict[str, Any]] = d.get("offers") or []
            return offers

        reachable = min(total, CFG.max_pages * CFG.page_size)
        offsets = list(range(CFG.page_size, reachable, CFG.page_size))
        if offsets:
            for chunk in await asyncio.gather(*(_page(o) for o in offsets)):
                items.extend(chunk)
        if total > CFG.max_pages * CFG.page_size:
            # Усечение обхода не молчит — то же правило, что в himalayas::collect.
            log.warning("getmatch: обход УПЁРСЯ В ЛИМИТ {} страниц (total={}) — часть вакансий "
                        "не собрана", CFG.max_pages, total)
        # Недобор БЕЗ сбойных страниц здесь не карается и порталу прощается отдельно:
        # `total` включает промо-анонсы и меняется между запросами.
        check_list_complete(len(items), total=total, per_page=CFG.page_size, failed=failed,
                            source="getmatch", max_lost_ratio=CFG.list_loss_max_ratio,
                            failed_kind="offset'ы")

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

        async def _fetch_full(it: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            async with esem:
                full = await self._get_one(it.get("id"))
            return it, full

        def _from_cache(pair: tuple[dict[str, Any], dict[str, Any]]) -> VacancyRecord:
            it, hit = pair
            return _normalize(it, cached_desc=hit["description_html"], enriched_at=hit.get("at"),
                              cached_exp=_cached_experience(hit))

        def _from_full(pair: tuple[dict[str, Any], dict[str, Any] | None]) -> VacancyRecord:
            return _normalize(pair[0], pair[1])

        fetched = list(await asyncio.gather(*(_fetch_full(it) for it in to_enrich))) if to_enrich else []
        # Нормализация — через normalize_each: кривая карточка портала не должна ронять
        # источник целиком (иначе санити-гейт видит нулевой срез и морозит кеш всех порталов).
        out = normalize_each(reuse, _from_cache, source="getmatch")
        enriched = normalize_each(fetched, _from_full, source="getmatch")
        out += enriched
        # без карточки — доберём следующим прогоном
        out += normalize_each(list_only, _normalize, source="getmatch")
        log.info("getmatch: собрано {} (кеш {}, дозагружено {}, только список {})",
                 len(out), len(reuse), len(enriched), len(list_only))
        return out
