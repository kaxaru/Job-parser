"""Источник devitjobs.uk — британская IT-доска. Вся выдача ОДНИМ запросом.

Самый простой адаптер в наборе: ни пагинации, ни реестра, ни второй фазы — эндпоинт
`jobsLight` отдаёт весь активный срез разом (2058 записей на 13.08.2026, ~3 МБ).

Зачем источник нужен — и чего от него ждать НЕ надо. Брали его ради `hasVisaSponsorship`:
ни один другой портал в сборе не отвечает на вопрос «спонсируют ли визу». Замер 13.08.2026
эту ставку не подтвердил — «Yes» стоит у 2 карточек из 2058, у остальных «No». Поле честное
и работает, но релокационным источником портал от этого не становится, и продавать его как
визовый нельзя.

Ценность оказалась в другом и она реальна: 1316 IT-вакансий после отсева, у 1029 из них
вилка, у ВСЕХ — грейд. По объёму это больше getmatch (606), themuse (1153) и jobicy (133),
а по заполненности полей — лучше любого из глобальных, где вилки нет вовсе.

Чего эндпоинт НЕ отдаёт и что из этого следует:
  * ОПИСАНИЯ НЕТ ВООБЩЕ — на то он и «light». Детект стека держится на структурных полях
    `technologies` (у карточек их по 10-20), `filterTags` и `techCategory`, и по замеру
    этого достаточно. Как следствие `enriched=False` у всех записей: в ленте карточка
    покажет стек и вилку, но не рассказ о вакансии;
  * валюты в схеме нет ни одним полем -> фунты по происхождению портала, см. _salary.

Ключа и авторизации не требуется (проверено 13.08.2026).
Автоотклик неприменим: заявка уходит на сайт работодателя.
"""
import json
from typing import Any

from hrwork.config import DEVITJOBS_URL, GLOBAL_SOURCES_IT_ONLY, log
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary, SalaryPeriod
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net.http import fetch_bytes
from hrwork.infrastructure.storage import VacancyRecord

from .base import Source, normalize_each, register_source
from .hh import BROWSER_UA

SITE = "https://devitjobs.uk"
# Валюта портала. В схеме её НЕТ ни одним полем, а вилки приходят голыми числами
# (27100, 40000, 130000) — это годовые фунты по происхождению площадки. Константа
# объявлена явно, чтобы допущение было видно в коде, а не растворилось в литерале.
CURRENCY = "GBP"

# `workplace` портала -> доменный формат. Берётся именно он, а не `remoteType`: последний
# заполнен двумя значениями (None и «onlycountry») и отвечает на другой вопрос —
# «ограничена ли удалёнка страной», а не «удалёнка ли это».
_WORKPLACE = {
    "remote": Schedule.REMOTE,
    "hybrid": Schedule.HYBRID,
    "office": Schedule.OFFICE,
}


def _salary(it: dict[str, Any]) -> Salary | None:
    """Годовая вилка в фунтах -> месячная (пересчёт делает домен, /12).

    Период задан ЯВНО и жёстко, без `SalaryPeriod.from_code`: поля периода в схеме нет,
    имена полей сами называют его (`annualSalaryFrom`/`annualSalaryTo`), и порядок величин
    это подтверждает. Инференс по величине (`SalaryPeriod.infer`) здесь был бы хуже —
    он гадает там, где схема уже ответила.

    gross=False: британская вилка объявляется до налогов, но НДФЛ 13 % к ней неприменим —
    там своя система (та же оговорка, что у himalayas и ashby)."""
    return Salary.monthly(it.get("annualSalaryFrom"), it.get("annualSalaryTo"),
                          CURRENCY, SalaryPeriod.YEAR)


def _visa(it: dict[str, Any]) -> bool:
    """Спонсирует ли работодатель визу.

    Поле приходит СТРОКОЙ «Yes»/«No», а не булевым: у всех 2058 записей оно непустое,
    поэтому `bool(...)` дал бы True на каждой карточке — включая явные «No»."""
    return str(it.get("hasVisaSponsorship") or "").strip().lower() == "yes"


def _summary(it: dict[str, Any], techs: str) -> str:
    """Текст карточки вместо описания, которого у эндпоинта нет.

    Собирается из структурных полей, и первым идёт визовое спонсорство: это единственное
    в наборе поле, которого нет ни у одного другого источника, и в ленте оно должно быть
    видно без открытия вакансии."""
    parts = [
        "Виза: спонсируют" if _visa(it) else "Виза: не спонсируют",
        str(it.get("jobType") or ""),
        str(it.get("companyType") or ""),
        f"Компания: {it.get('companySize')}" if it.get("companySize") else "",
        techs,
    ]
    return " · ".join(p for p in parts if p)


def _normalize(it: dict[str, Any]) -> VacancyRecord | None:
    """Карточка devitjobs -> VacancyRecord (ACL: внешняя схема живёт только здесь).
    `None` — штатный отсев снятой с публикации вакансии (`isPaused`)."""
    if it.get("isPaused"):
        return None
    name = str(it.get("name") or "")
    techs = ", ".join(str(t) for t in (it.get("technologies") or []) if t)
    tags = " ".join(str(t) for t in (it.get("filterTags") or []) if t)
    when = str(it.get("activeFrom") or "") or None
    slug = str(it.get("jobUrl") or "")
    vac = build_vacancy(
        vid=f"devitjobs_{it.get('_id')}",
        name=name,
        city=str(it.get("actualCity") or "") or REMOTE_CITY,
        city_id="",
        salary=_salary(it),
        # Грейд и тип занятости уходят в домен ЯРЛЫКАМИ: «Regular» (британское имя
        # середины) и «Internship» узнаёт общая таблица грейдов, остальное даёт None.
        experience=Experience.from_grades([it.get("expLevel"), it.get("jobType")]),
        schedule=_WORKPLACE.get(str(it.get("workplace") or "").lower(), Schedule.OFFICE),
        # Описания у эндпоинта нет — детект стека держится на структурных полях.
        detect_text=f"{name} {techs} {tags} {it.get('techCategory') or ''}",
        employer=str(it.get("company") or ""),
        created_at=when,
        published_at=when,
        responses=None,
        source="devitjobs",
    )
    return VacancyRecord(vacancy=vac, url=f"{SITE}/jobs/{slug}" if slug else SITE,
                         description_html="", requirement=_summary(it, techs),
                         sig=when or "",
                         enriched=False,           # полного описания у источника не бывает
                         enriched_at=None)


@register_source("devitjobs")
class DevitjobsSource(Source):
    """Сбор вакансий devitjobs: один запрос, одна фаза."""

    name = "devitjobs"

    def __init__(self, **_: Any) -> None:
        pass

    async def collect(self) -> list[VacancyRecord]:
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        out = await fetch_bytes(DEVITJOBS_URL, headers=headers)
        if not out:
            log.warning("devitjobs: выдача не получена — источник недоступен")
            return []
        try:
            items: list[dict[str, Any]] = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            log.warning("devitjobs: битый JSON ({}) — источник пропущен", e)
            return []
        if not isinstance(items, list):
            log.warning("devitjobs: ожидался список, пришло {} — источник пропущен",
                        type(items).__name__)
            return []

        paused = sum(1 for it in items if isinstance(it, dict) and it.get("isPaused"))
        recs = normalize_each(items, _normalize, source="devitjobs")
        out_recs = [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs
        # Счётчик виз — по СЫРЫМ карточкам, а не по тексту `requirement`: считать по своей
        # же подписи значит завязать метрику на формат строки, и первая правка `_summary`
        # молча обнулила бы её.
        kept = {r.id for r in out_recs}
        visa = sum(1 for it in items if isinstance(it, dict)
                   and f"devitjobs_{it.get('_id')}" in kept and _visa(it))
        log.info("devitjobs: собрано {} (карточек {}, снятых с публикации {}, "
                 "не-IT отсеяно {}, со спонсорством визы {})",
                 len(out_recs), len(items), paused, len(recs) - len(out_recs), visa)
        return out_recs
