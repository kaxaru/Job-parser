"""Источник himalayas.app — JSON-API вакансий с ПОЛНОЙ удалёнкой (global remote).

Второй не-русскоязычный портал после arbeitnow. Данные богаче: приходят грейд (`seniority`),
вилка с валютой и периодом, тип занятости и — главное — `locationRestrictions`: список стран,
из которых работодатель готов нанимать. Это ровно то, чего нет больше нигде: «remote» на
глобальном рынке в большинстве случаев значит «remote в пределах США», и без этого поля лента
забивается вакансиями, куда откликаться бессмысленно. Пишем его в город.

Сбор ОДНОФАЗНЫЙ: `description` (HTML) приходит в списке. Пагинация через `offset`, страница
жёстко 20 записей — `limit` больше 20 портал игнорирует (проверено 07.08.2026), поэтому
страниц много и они тянутся пачками.

ЗАРПЛАТА ПРИВОДИТСЯ К МЕСЯЧНОЙ. У портала `salaryPeriod` = annual (88 %), hourly (9 %),
monthly (3 %), а весь наш домен — от аналитики до фильтров ленты — считает вилку месячной.
Годовые 130 000 USD без пересчёта встали бы рядом с месячными рублями и сломали бы и медианы,
и сортировку. Пересчёт: annual / 12, hourly × HOURS_PER_MONTH. Это ОЦЕНКА (у часовой ставки
реальная загрузка неизвестна), поэтому она названа константой, а не спрятана в выражении.

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
    HIMALAYAS_MAX_PAGES,
    HIMALAYAS_PAGE_CONCURRENCY,
    log,
)
from hrwork.domain.experience import Experience
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
from .hh import BROWSER_UA

SITE = "https://himalayas.app"
API = f"{SITE}/jobs/api"
PAGE_SIZE = 20            # портал игнорирует limit>20 — размер страницы задан им, не нами
HOURS_PER_MONTH = 160     # 40 ч/нед × 4 — оценка для пересчёта часовой ставки в месячную


@dataclass(frozen=True)
class HimalayasCfg:
    api_url: str = API
    page_size: int = PAGE_SIZE
    max_pages: int = HIMALAYAS_MAX_PAGES
    page_conc: int = HIMALAYAS_PAGE_CONCURRENCY


CFG = HimalayasCfg()

# Грейд himalayas -> Experience. Шкала своя (Entry-level/Mid-level/Senior/Manager/Director/
# Executive), поэтому маппится явно, а не через _GRADE_TO_EXP: у нас там trainee/junior/
# middle/senior/lead. Границы выровнены с getmatch/hirify, чтобы один и тот же «senior»
# с любого портала попадал в одну корзину — иначе поедут и отбор, и срезы аналитики.
_SENIORITY_TO_EXP: list[tuple[str, Experience]] = [
    ("entry-level", Experience.NONE),
    ("mid-level",   Experience.BETWEEN_1_3),
    ("senior",      Experience.BETWEEN_3_6),
    ("manager",     Experience.MORE_6),
    ("director",    Experience.MORE_6),
    ("executive",   Experience.MORE_6),
]


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


def _experience(it: dict[str, Any]) -> Experience | None:
    """Грейд -> уровень опыта. `seniority` — СПИСОК (бывает ['Entry-level', 'Mid-level']),
    берём самый МЛАДШИЙ: так же поступает Experience.from_hirify_grades, и по той же
    причине — вакансия с вилкой грейдов открыта и для младшего."""
    names = {str(s).strip().lower() for s in (it.get("seniority") or [])}
    for grade, exp in _SENIORITY_TO_EXP:        # порядок = от младшего к старшему
        if grade in names:
            return exp
    return None


def _salary(it: dict[str, Any]) -> Salary | None:
    """Вилка -> VO, приведённая к МЕСЯЧНОЙ (см. шапку модуля). Валюта сохраняется как есть —
    конвертацию в рубли делает Salary/rates, это не забота адаптера. Период неизвестен ->
    вилку не берём: лучше пусто, чем значение, отличающееся от истины в 12 раз."""
    frm, to = it.get("minSalary"), it.get("maxSalary")
    if frm is None and to is None:
        return None
    period = str(it.get("salaryPeriod") or "").strip().lower()
    if period == "annual":
        div = 12.0
    elif period == "monthly":
        div = 1.0
    elif period == "hourly":
        div = 1.0 / HOURS_PER_MONTH
    else:
        return None
    def _scale(x: Any) -> int | None:
        try:
            return int(float(x) / div)
        except (TypeError, ValueError):
            return None
    f, t = _scale(frm), _scale(to)
    if f is None and t is None:
        return None
    # gross=False: портал не размечает налоги, а вычитать 13 % НДФЛ из зарубежной вилки
    # было бы враньём — там своя налоговая система.
    return Salary(f, t, it.get("currency"), gross=False)


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
        return "Worldwide"
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
        experience=_experience(it),
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

    async def _get_page(self, offset: int) -> list[dict[str, Any]]:
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

    async def collect(self) -> list[VacancyRecord]:
        first = await self._get_page(0)
        if not first:
            log.warning("himalayas: первая страница пуста — источник недоступен")
            return []

        items: list[dict[str, Any]] = list(first)
        page = 1
        while page < CFG.max_pages:
            offsets = [(page + i) * CFG.page_size
                       for i in range(CFG.page_conc)
                       if page + i < CFG.max_pages]
            chunks = await asyncio.gather(*(self._get_page(o) for o in offsets))
            items.extend(x for c in chunks for x in c)
            if any(not c for c in chunks):       # встретилась пустая — выдача кончилась
                break
            page += len(offsets)

        # Выдача идёт по дате и может сдвинуться между запросами -> одна вакансия на двух
        # страницах. Дедуп по guid (он же основа id).
        uniq: dict[str, dict[str, Any]] = {}
        for it in items:
            vid = _vid(it)
            if vid:
                uniq.setdefault(vid, it)
        recs = [_normalize(it) for it in uniq.values()]
        # Портал общий, не IT-шный (63 % не-IT). См. config.GLOBAL_SOURCES_IT_ONLY.
        out = [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs
        log.info("himalayas: собрано {} (страниц {}, дублей {}, не-IT отсеяно {})",
                 len(out), page, len(items) - len(uniq), len(recs) - len(out))
        return out
