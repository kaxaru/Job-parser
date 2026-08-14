"""Источник greenhouse.io — job-борды работодателей, а не общая доска объявлений.

Первый источник такого класса: у Greenhouse нет единой выдачи, есть борд каждой компании
по слагу (`/v1/boards/{org}/jobs`). Охват задаёт наш реестр `config.GREENHOUSE_BOARDS`,
механика обхода — общая для ATS, в `ats.py`.

Зачем при живых агрегаторах. Замер 13.08.2026 по 53 бордам обеих платформ: у нас в кеше
лежало 1892 вакансии этих компаний, на их бордах — 8836. Работодатель выкладывает на
собственный борд ВСЁ, на сторонние площадки — единицы (databricks 807 против наших 147,
stripe 566 против 38, openai 731 против 3).

Сбор ОДНОФАЗНЫЙ: `?content=true` отдаёт полное описание прямо в списке.

Чего API НЕ отдаёт и что из этого следует:
  * зарплаты нет. Поле `pay_input_ranges` в схеме есть, но по замеру пустое: 0 из 807
    карточек databricks. Поэтому `salary=None` — брать поле неизвестной формы вслепую
    хуже, чем честно не иметь вилки;
  * грейда нет ни полем, ни списком -> `experience=None`. Выдумывать из тайтла нельзя:
    поедет относительно порталов, где грейд приходит полем (см. Experience.from_getmatch);
  * формата работы отдельным полем тоже нет — только строка `location.name`, см. _schedule.

ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ. Описание у большинства компаний начинается с одинакового блока
о самой компании (`<div class="content-intro">`), поэтому `requirement` — а это подпись
под карточкой в ленте — у всех вакансий одного работодателя выходит один и тот же
(«GitLab is the intelligent orchestration platform…»). Вырезать интро не стали: разметка
блока у каждой компании своя, а неудачная эвристика съела бы вместе с ним начало реального
описания. В модалке ленты текст полный, потери только в подписи.

Ключа и авторизации не требуется (проверено 13.08.2026).
Автоотклик неприменим: заявка уходит в форму на борде работодателя.
"""
import html
from typing import Any

from hrwork.config import GLOBAL_SOURCES_IT_ONLY, GREENHOUSE_BOARDS, log
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.parsing import build_vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.storage import VacancyRecord

from .ats import collect_boards
from .base import Source, normalize_each, register_source
from .text import strip_html

SITE = "https://boards-api.greenhouse.io"
API = f"{SITE}/v1/boards"


def board_url(org: str) -> str:
    """URL борда компании. `content=true` включает описание — иначе понадобилась бы
    вторая фаза на каждую вакансию, а их тысячи."""
    return f"{API}/{org}/jobs?content=true"


def _jobs(payload: Any) -> list[dict[str, Any]]:
    return list((payload or {}).get("jobs") or [])


def _text(content: Any) -> str:
    """`content` -> плоский текст. Порядок операций важен и стоил бы молчаливой потери
    всего детекта стека.

    Greenhouse отдаёт описание ДВАЖДЫ экранированным: в JSON лежит не HTML, а его
    entity-представление (`&lt;div class=&quot;content-intro&quot;&gt;`). Снять сначала
    теги, как у остальных источников, здесь нечего — тегов в строке нет, есть текст,
    похожий на теги. Поэтому сперва `unescape` (получаем настоящий HTML), и только потом
    общий стриппер.
    """
    return strip_html(html.unescape(str(content or "")))


def _city(it: dict[str, Any]) -> str:
    """`location.name` — свободная строка работодателя («Remote, Italy», «San Francisco»,
    «Remote - Americas»). Пусто -> доменная REMOTE_CITY: борды сплошь у remote-first
    компаний, и отдельный бакет пустого города в фасете ленты нам не нужен."""
    name = " ".join(str((it.get("location") or {}).get("name") or "").split())
    return name or REMOTE_CITY


def _schedule(it: dict[str, Any]) -> Schedule:
    """Формат работы — ТОЛЬКО из строки локации, отдельного поля у API нет.

    Осознанное огрубление: «Remote, Italy» и «Remote - Americas» станут REMOTE, хотя это
    удалёнка С ОГРАНИЧЕНИЕМ по стране. Ограничение не теряется — оно остаётся в `city`
    целиком, ровно как у himalayas, где страны найма тоже живут в городе. HYBRID не
    распознаётся вовсе: словом «hybrid» строка локации не размечается."""
    return Schedule.REMOTE if "remote" in _city(it).lower() else Schedule.OFFICE


def _sig(it: dict[str, Any]) -> str:
    """Маркер изменения карточки — `updated_at` (у портала он настоящий, в отличие от
    arbeitnow, где пришлось брать дату публикации)."""
    return str(it.get("updated_at") or "")


def _normalize(it: dict[str, Any]) -> VacancyRecord:
    """Карточка greenhouse -> VacancyRecord (ACL: внешняя схема живёт только здесь)."""
    name = str(it.get("title") or "")
    desc = _text(it.get("content"))
    deps = " ".join(str((d or {}).get("name") or "") for d in (it.get("departments") or []))
    # created_at = ПЕРВАЯ публикация, а не updated_at: возраст вакансии в ленте должен
    # считаться от появления, иначе правка описания молодила бы старую вакансию.
    first = str(it.get("first_published") or "") or None
    vac = build_vacancy(
        vid=f"greenhouse_{it.get('id')}",        # id портала уникален между бордами
        name=name,
        city=_city(it),
        city_id="",
        salary=None,                             # вилки в выдаче нет — см. шапку модуля
        experience=None,                         # грейда в выдаче нет — см. шапку модуля
        schedule=_schedule(it),
        detect_text=f"{name} {deps} {desc}",
        employer=str(it.get("company_name") or ""),
        created_at=first,
        published_at=first,                      # переоткрытий портал не отмечает
        responses=None,
        source="greenhouse",
    )
    return VacancyRecord(vacancy=vac, url=str(it.get("absolute_url") or ""),
                         description_html=desc, requirement=desc[:600],
                         sig=_sig(it), enriched=bool(desc), enriched_at=None)


@register_source("greenhouse")
class GreenhouseSource(Source):
    """Сбор вакансий greenhouse: обход реестра бордов, одна фаза."""

    name = "greenhouse"

    def __init__(self, **_: Any) -> None:
        pass                                     # ключа и прокси не нужно — публичный API

    async def collect(self) -> list[VacancyRecord]:
        alive = await collect_boards(GREENHOUSE_BOARDS, board_url, _jobs, source="greenhouse")
        if not alive:
            return []

        # Дедуп по id портала: одна вакансия может висеть на борде дважды (разные офисы —
        # у Greenhouse это ОТДЕЛЬНЫЕ карточки с разными id, а вот повтор одного id внутри
        # борда встречается на мультилокационных публикациях).
        uniq: dict[str, dict[str, Any]] = {}
        total = 0
        for jobs in alive.values():
            total += len(jobs)
            for it in jobs:
                if it.get("id") is not None:
                    uniq.setdefault(str(it["id"]), it)
        recs = normalize_each(uniq.values(), _normalize, source="greenhouse")
        # Борд работодателя — НЕ IT-выдача: техкомпании публикуют там продажи, саппорт
        # и юристов на тех же страницах. Замер 13.08.2026 по обоим ATS: 43 % бесспорно
        # не-IT (Account Executive, BDR, Accounting Manager). См. GLOBAL_SOURCES_IT_ONLY.
        out = [r for r in recs if r.vacancy.role.is_it] if GLOBAL_SOURCES_IT_ONLY else recs
        log.info("greenhouse: собрано {} (бордов {} из {}, карточек {}, дублей {}, "
                 "не-IT отсеяно {})", len(out), len(alive), len(GREENHOUSE_BOARDS),
                 total, total - len(uniq), len(recs) - len(out))
        return out
