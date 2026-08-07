"""Источник web3.career — JSON-API вакансий web3/крипто-рынка.

Третий глобальный портал после arbeitnow и himalayas. Специфика домена: Solidity/Rust/
смарт-контракты, но нам он интересен другим — бэкендом и дата-инженерией в крипто-компаниях
(Coinbase, Kraken, Tether, Bitfinex, Ripple, Polymarket, Binance). Замер 07.08.2026 по 17
тегам: 957 уникальных вакансий, из них 326 remote и 272 с вилкой.

**ТРЕБУЕТ ТОКЕНА** (`WEB3_TOKEN`, бесплатный, по email на https://web3.career/web3-jobs-api).
Без него источник молча пропускается: анонимный вызов редиректит на форму регистрации.

ПАГИНАЦИИ У API НЕТ, и это определяет всю схему сбора. Документированы только `remote`,
`limit` (максимум 100), `country`, `tag`, `show_description`; проверены и отвергнуты `page`,
`offset`, `skip`, `start`, `p` — все пятеро возвращают ту же первую сотню. Поэтому охват
набирается ПЕРЕБОРОМ ТЕГОВ: каждый тег отдаёт свои до ста, объединение даёт покрытие.
Список тегов задан явно (`WEB3_TAGS`), а не тянется с главной: разметка портала может
измениться в любой момент, и молча опустевший список тегов означал бы молча опустевший сбор.

Описание запрашивается (`show_description=true`) — оно приходит в том же ответе, отдельной
карточки у API нет, поэтому сбор однофазный.

ЛОВУШКА: документированный `show_description=false` ОБНУЛЯЕТ выдачу — не убирает описания,
а возвращает пустой список (проверено 07.08.2026: `tag=python` даёт 100 вакансий, он же с
`show_description=false` — ноль). Параметр не использовать даже ради экономии трафика:
внешне это неотличимо от «по тегу ничего нет», и сбор молча собрал бы пустоту.

Автоотклик неприменим: заявка уходит по внешнему `apply_url` на сайт работодателя.
"""
import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any

from hrwork.config import (
    GLOBAL_SOURCES_IT_ONLY,
    WEB3_TAG_CONCURRENCY,
    WEB3_TAGS,
    WEB3_TOKEN,
    log,
)
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary, SalaryPeriod
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, register_source
from .hh import BROWSER_UA

SITE = "https://web3.career"
API = f"{SITE}/api/v1"
PAGE_LIMIT = 100          # жёсткий потолок API: limit больше портал не отдаёт



@dataclass(frozen=True)
class Web3Cfg:
    api_url: str = API
    limit: int = PAGE_LIMIT
    tag_conc: int = WEB3_TAG_CONCURRENCY
    retry_attempts: int = 3
    backoff_start: float = 1.0
    backoff_max: float = 8.0


CFG = Web3Cfg()


def _iso(ts: Any) -> str | None:
    """date_epoch приходит unix-секундами -> ISO-UTC."""
    try:
        return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _sig(it: dict[str, Any]) -> str:
    return str(it.get("date_epoch") or "")


def _salary(it: dict[str, Any]) -> Salary | None:
    """Вилка -> VO, приведённая к МЕСЯЧНОЙ доменной фабрикой.

    `salary_unit` заполнен лишь у ~5 % записей (замер 07.08.2026: HOUR у 2 и YEAR у 3 из
    100), при том что сами значения есть. Поэтому период отдаём как `None` — домен сам
    определит его по величине (`SalaryPeriod.infer`). Раньше здесь стоял свой порог
    (15 000) против 25 000 у hirify: один и тот же вопрос имел два разных ответа.

    ВАЛЮТА НЕ ВЫДУМЫВАЕТСЯ. Она отсутствует у ~94 % записей, и на крипто-рынке платят
    и в USD, и в стейблкоинах — проставить USD по умолчанию значило бы врать рублёвой
    аналитике (`Salary.to_rub` сконвертировал бы по курсу то, что валютой не является).
    Вилка без валюты сохраняется с currency=None: величина видна в карточке, а в срезы
    по деньгам такая запись не попадёт.

    gross=False: портал не размечает налоги, и вычитать НДФЛ 13 % из зарубежной вилки
    было бы враньём — там своя налоговая система (то же решение, что в himalayas)."""
    frm, to = it.get("salary_min_value"), it.get("salary_max_value")
    # Инференс запрашивается ЯВНО: домен не решает за адаптер, можно ли угадывать период —
    # это знание о качестве данных конкретного портала. Здесь угадывать оправдано (поле
    # заполнено у ~5 %), у himalayas — нет (оно есть почти везде, и пустое там аномалия).
    period = SalaryPeriod.from_code(it.get("salary_unit")) or SalaryPeriod.infer(_probe(frm, to))
    return Salary.monthly(frm, to, it.get("salary_currency"), period)


def _probe(frm: Any, to: Any) -> float | None:
    """Величина для инференса периода — первая непустая граница вилки."""
    for x in (frm, to):
        if x is not None:
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
    return None


def _city(it: dict[str, Any]) -> str:
    """Город из city/location, иначе страна. Пусто у полностью удалённой -> 'Remote'."""
    for key in ("city", "location", "country"):
        val = str(it.get(key) or "").strip()
        if val:
            return val.title() if val.islower() else val
    return REMOTE_CITY


def _normalize(it: dict[str, Any]) -> VacancyRecord:
    """Карточка web3.career -> VacancyRecord (ACL: внешняя схема живёт только здесь)."""
    name = str(it.get("title") or "")
    desc = str(it.get("description") or "")
    tags = " ".join(str(t) for t in (it.get("tags") or []))
    when = _iso(it.get("date_epoch"))
    vac = build_vacancy(
        vid=f"web3_{it.get('id')}",              # неймспейс — не сталкивается с id других порталов
        name=name,
        city=_city(it),
        city_id="",
        salary=_salary(it),
        # Грейда отдельным полем нет. В `tags` встречаются junior/entry-level/lead, но это
        # СВОБОДНЫЕ метки работодателя вперемешку со стеком и бенефитами, а не шкала —
        # выводить из них Experience значило бы разъехаться с hirify/getmatch, где грейд
        # приходит полем. Честнее None: отбор под отклик и так смотрит на тайтл.
        experience=None,
        schedule=Schedule.REMOTE if it.get("is_remote") else Schedule.OFFICE,
        detect_text=f"{name} {tags} {desc}",
        employer=str(it.get("company") or ""),
        created_at=when,
        published_at=when,
        responses=None,
        source="web3",
    )
    return VacancyRecord(vacancy=vac, url=str(it.get("apply_url") or ""),
                         description_html=desc, requirement=desc[:600],
                         sig=_sig(it), enriched=bool(desc), enriched_at=None)


@register_source("web3")
class Web3CareerSource(Source):
    """Сбор вакансий web3.career: перебор тегов (пагинации у API нет)."""

    name = "web3"

    def __init__(self, **_: Any) -> None:
        pass

    async def _get_tag(self, tag: str) -> list[dict[str, Any]]:
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        url = (f"{CFG.api_url}?token={WEB3_TOKEN}&limit={CFG.limit}"
               f"&tag={tag}&show_description=true")
        delay = CFG.backoff_start
        for _attempt in range(CFG.retry_attempts):
            out = await fetch_bytes(url, headers=headers)
            if out:
                try:
                    payload = json.loads(out.decode("utf-8", "replace"))
                except json.JSONDecodeError as e:
                    log.debug("web3 tag={}: {}", tag, e)
                    payload = None
                if isinstance(payload, list):
                    # Ответ — [строка-заголовок, строка-справка, [вакансии]]: берём
                    # первый вложенный список, а не индекс, чтобы не сломаться от
                    # перестановки элементов в ответе.
                    for el in payload:
                        if isinstance(el, list):
                            return [x for x in el if isinstance(x, dict)]
                    return []
            await asyncio.sleep(delay)
            delay = min(delay * 2, CFG.backoff_max)
        return []

    async def collect(self) -> list[VacancyRecord]:
        if not WEB3_TOKEN:
            log.info("web3: WEB3_TOKEN не задан — источник пропущен "
                     "(токен бесплатный: https://web3.career/web3-jobs-api)")
            return []
        if not WEB3_TAGS:
            log.warning("web3: список тегов пуст — собирать нечего")
            return []

        sem = asyncio.Semaphore(CFG.tag_conc)

        async def _one(tag: str) -> list[dict[str, Any]]:
            async with sem:
                return await self._get_tag(tag)

        chunks = await asyncio.gather(*(_one(t) for t in WEB3_TAGS))

        # Теги пересекаются по построению (одна вакансия имеет и python, и backend, и
        # remote), поэтому дедуп по id обязателен — без него запись размножилась бы по
        # числу своих тегов.
        uniq: dict[Any, dict[str, Any]] = {}
        total = 0
        for chunk in chunks:
            total += len(chunk)
            for it in chunk:
                if it.get("id") is not None:
                    uniq.setdefault(it["id"], it)
        recs = [_normalize(it) for it in uniq.values()]
        out = [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs
        log.info("web3: собрано {} (тегов {}, ответов {}, дублей {}, не-IT отсеяно {})",
                 len(out), len(WEB3_TAGS), total, total - len(uniq), len(recs) - len(out))
        return out
