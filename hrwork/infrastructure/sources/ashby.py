"""Источник ashbyhq.com — job-борды работодателей (второй ATS, устройство как у greenhouse).

Охват задаёт реестр `config.ASHBY_BOARDS`, обход — общий `ats.py`. Платформа новее
Greenhouse и собрала на себе свежую AI-волну: openai, cursor, elevenlabs, replit,
supabase. Замер 13.08.2026: 2440 вакансий с 21 борда, из них у нас в кеше было 3 (openai),
2 (cursor), 12 (elevenlabs) — то есть агрегаторы этот пласт практически не видят.

Сбор ОДНОФАЗНЫЙ: `descriptionPlain` и `descriptionHtml` приходят в списке.

Что схема отдаёт лучше остальных и что из этого НЕ следует:
  * `compensation` — структурная вилка с валютой и интервалом (не строкой, как сводка
    `compensationTierSummary`), см. _salary;
  * `workplaceType` — честный трёхзначный формат, ложится на домен один в один;
  * `employmentType` (FullTime/Contract/Intern) в `Employment` НЕ идёт — это другая ось,
    см. _experience.

Имени компании в ответе нет вовсе (`name` на верхнем уровне пуст), поэтому работодатель
восстанавливается из слага — `ats.pretty_company`.

`publishedAt` — ЕДИНСТВЕННОЕ поле даты в схеме, и оно означает первую публикацию. У части
вакансий это 2021-2022 год: вечнозелёные позиции, которые компания держит открытыми годами
(проверено 13.08.2026). Поднимать их датой сбора нельзя — свежесть в ленте перестала бы
что-либо значить, поэтому они честно уезжают в «гостов». Массовой проблемы это не создаёт:
из 1107 карточек пяти бордов 979 опубликованы в 2026 году, старше 2024-го — всего 7.
Распределение то же, что у greenhouse (874 из 1091 за 2026), то есть это свойство ATS,
а не дефект конкретной платформы.

Ключа и авторизации не требуется (проверено 13.08.2026).
Автоотклик неприменим: заявка уходит в форму на борде работодателя.
"""
from functools import partial
from typing import Any

from hrwork.config import ASHBY_BOARDS, GLOBAL_SOURCES_IT_ONLY, log
from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.salary import Salary, SalaryPeriod
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.storage import VacancyRecord

from .ats import collect_boards, pretty_company
from .base import Source, normalize_each, register_source

SITE = "https://jobs.ashbyhq.com"
API = "https://api.ashbyhq.com/posting-api/job-board"

# `workplaceType` портала -> доменный формат. Совпадение полное, поэтому это словарь,
# а не эвристика по подстроке, как пришлось делать в greenhouse.
_WORKPLACE = {
    "Remote": Schedule.REMOTE,
    "Hybrid": Schedule.HYBRID,
    "OnSite": Schedule.OFFICE,
}


def board_url(org: str) -> str:
    """URL борда. `includeCompensation=true` добавляет вилку — без флага её нет вовсе."""
    return f"{API}/{org}?includeCompensation=true"


def _jobs(payload: Any) -> list[dict[str, Any]]:
    return list((payload or {}).get("jobs") or [])


def _schedule(it: dict[str, Any]) -> Schedule:
    """Формат работы — по `workplaceType`, а НЕ по `isRemote`.

    Флаг `isRemote` у портала означает «есть удалённая составляющая» и приходит `true`
    в том числе при `workplaceType: Hybrid` (проверено на выдаче ramp 13.08.2026: одна
    и та же карточка — isRemote=true, workplaceType=Hybrid, location «New York, NY (HQ)»).
    Поверить флагу значило бы записать гибрид в удалёнку, а это ровно тот фильтр, по
    которому лента отбирает вакансии, куда можно откликаться из другой страны."""
    return _WORKPLACE.get(str(it.get("workplaceType") or ""), Schedule.OFFICE)


def _experience(it: dict[str, Any]) -> Experience | None:
    """Грейда у портала нет; единственный намёк — `employmentType`.

    В домен уходит как ЯРЛЫК, а решает домен: из набора FullTime/PartTime/Contract/Intern
    в таблице грейдов узнаётся только `intern` -> опыта не требуется, остальные дают None
    (штатный контракт `Experience.from_grades`). Разбирать этот набор здесь, в адаптере,
    нельзя — словарь грейдов единый на все порталы (см. шапку domain/experience.py)."""
    return Experience.from_grades([it.get("employmentType")])


def _period(interval: Any) -> SalaryPeriod | None:
    """`interval` портала («1 YEAR», «1 MONTH», «NONE») -> доменный период.

    Формат — «МНОЖИТЕЛЬ ЕДИНИЦА», и множитель приходится разбирать здесь: домен знает
    слова («year»/«month»/«hour»), а не диалект конкретного портала.

    Множитель, отличный от единицы, ОТВЕРГАЕТСЯ вместе со всей вилкой. В выдаче он не
    встречался (13.08.2026), но отдать «2 YEAR» домену как год значит ошибиться вдвое,
    а ошибка в периоде — ошибка в разы, она хуже пустого поля. То же правило, что
    у himalayas на неизвестном `salaryPeriod`."""
    words = str(interval or "").split()
    if len(words) == 2 and words[0] != "1":
        return None
    return SalaryPeriod.from_code(words[-1]) if words else None


def _salary(it: dict[str, Any]) -> Salary | None:
    """Вилка из СТРУКТУРНЫХ компонентов, а не из сводки `compensationTierSummary`.

    Сводка — человеческая строка («$211.4K – $290.6K • Offers Equity»), где сумма
    округлена до сотен и смешана с опционами; парсить её значило бы городить свой
    разбор валют и множителей. Внутри `compensationTiers[].components[]` лежит то же
    самое числами: `compensationType`, `minValue`, `maxValue`, `currencyCode`, `interval`.

    Берётся компонент с `compensationType == "Salary"` — рядом лежат `EquityPercentage`
    и бонусы, и сложить их в вилку было бы враньём. Период — через `_period`.

    gross=False (дефолт `Salary.monthly`): портал налоги не размечает, а вычитать 13 % НДФЛ
    было бы враньём — там своя налоговая система (та же оговорка, что у himalayas).
    """
    for tier in ((it.get("compensation") or {}).get("compensationTiers") or []):
        for comp in ((tier or {}).get("components") or []):
            if str((comp or {}).get("compensationType") or "") != "Salary":
                continue
            sal = Salary.monthly(comp.get("minValue"), comp.get("maxValue"),
                                 comp.get("currencyCode"), _period(comp.get("interval")))
            if sal is not None:
                return sal
    return None


def _city(it: dict[str, Any]) -> str:
    """Локация строкой + дополнительные площадки. Пусто -> доменная REMOTE_CITY."""
    main = " ".join(str(it.get("location") or "").split())
    return main or REMOTE_CITY


def _sig(it: dict[str, Any]) -> str:
    return str(it.get("publishedAt") or "")


def _normalize(it: dict[str, Any], *, org: str) -> VacancyRecord:
    """Карточка ashby -> VacancyRecord (ACL: внешняя схема живёт только здесь).

    `org` приходит извне: имени работодателя в схеме нет, и восстановить его можно
    только из слага борда, по которому карточка получена."""
    name = str(it.get("title") or "")
    desc_html = str(it.get("descriptionHtml") or "")
    desc = str(it.get("descriptionPlain") or "")
    team = " ".join(str(it.get(k) or "") for k in ("department", "team"))
    pub = str(it.get("publishedAt") or "") or None
    vac = build_vacancy(
        vid=f"ashby_{it.get('id')}",             # id портала — UUID, уникален глобально
        name=name,
        city=_city(it),
        city_id="",
        salary=_salary(it),
        experience=_experience(it),
        schedule=_schedule(it),
        detect_text=f"{name} {team} {desc}",
        employer=pretty_company(org),            # своего имени компании в схеме нет
        created_at=pub,
        published_at=pub,
        responses=None,
        source="ashby",
    )
    return VacancyRecord(vacancy=vac, url=str(it.get("jobUrl") or ""),
                         description_html=desc_html or desc, requirement=desc[:600],
                         sig=_sig(it), enriched=bool(desc or desc_html), enriched_at=None)


@register_source("ashby")
class AshbySource(Source):
    """Сбор вакансий ashby: обход реестра бордов, одна фаза."""

    name = "ashby"

    def __init__(self, **_: Any) -> None:
        pass

    async def collect(self) -> list[VacancyRecord]:
        alive = await collect_boards(ASHBY_BOARDS, board_url, _jobs, source="ashby")
        if not alive:
            return []

        # Нормализация ПОБОРДОВО, а не общим списком: работодатель берётся из слага, и
        # после слияния бордов в один список эта связь была бы потеряна.
        recs: list[VacancyRecord] = []
        total = 0
        seen: set[str] = set()
        for org, jobs in alive.items():
            total += len(jobs)
            fresh = [it for it in jobs
                     if it.get("id") is not None and str(it["id"]) not in seen]
            seen.update(str(it["id"]) for it in fresh)
            recs += normalize_each(fresh, partial(_normalize, org=org), source="ashby")
        out = [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs
        log.info("ashby: собрано {} (бордов {} из {}, карточек {}, дублей {}, "
                 "не-IT отсеяно {})", len(out), len(alive), len(ASHBY_BOARDS),
                 total, total - len(seen), len(recs) - len(out))
        return out
