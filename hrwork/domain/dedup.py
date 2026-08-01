"""Кросс-портальный дедуп: одна и та же вакансия, опубликованная на разных порталах.

Зачем. talanto и getmatch переопубликовывают объявления, которые уже есть на hh, дословно:
замер 01.08.2026 по кешу из 87 118 записей нашёл 3663 группы, где ОДИН И ТОТ ЖЕ
работодатель+тайтл приходит от двух и более источников.

Ключ — (работодатель, тайтл), оба нормализованные. Города в ключе НЕТ, и это главное
решение модуля. Интуиция подсказывает обратное, но замер показал: из 894 групп, где города
формально «разные», подавляющее большинство — та же вакансия с разной ГРАНУЛЯРНОСТЬЮ
локации. hh округляет до «Москва», talanto отдаёт точное поселение:

    Инженер-программист Linux | НПЦ ЭЛВИС     hh: Москва   talanto: Зеленоград
    Программист 1С:ERP        | МКБ «Факел»   hh: Москва   talanto: Химки
    Программист 1С            | Логопарк      hh: Москва   talanto: рабочий посёлок Быково

Город в ключе заблокировал бы эти склейки ради защиты от редкого случая «две РАЗНЫЕ
вакансии одного работодателя в двух городах» (ZennoLab: Москва + Санкт-Петербург).

ОСОЗНАННЫЙ КОМПРОМИСС: такие пары мы схлопнем и одну вакансию потеряем. Цена принята,
потому что победитель выбирается по приоритету источников, а первым идёт hh — портал,
с которого работает автоотклик. То есть теряется копия на второстепенном портале, куда
откликнуться всё равно нельзя. Подробности — docs/collect.md.

Грейд из тайтла НЕ вычищается: «Junior Python» и «Senior Python» у одного работодателя —
разные вакансии, склеивать их нельзя.
"""
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol, TypeVar

from hrwork.domain.models import Vacancy

# Организационно-правовые формы: «ООО Рога» и «Рога» — один работодатель.
_LEGAL = re.compile(
    r"\b(ооо|оао|зао|пао|ао|ип|нко|ано|фгуп|гуп|мбу|гбу|llc|ltd|inc|corp|gmbh|co)\b\.?", re.I)
# Уточнение в скобках — «(офис, Москва)», «(удалённо)», «(ПСБ Банк)»: у разных порталов
# оно своё, а вакансия одна.
_PARENS = re.compile(r"\(.*?\)")
_PUNCT = re.compile(r"[^\w\s]+", re.U)
_SPACE = re.compile(r"\s+")


def _norm(s: str | None) -> str:
    """Нижний регистр, ё->е, без пунктуации и лишних пробелов."""
    t = (s or "").lower().replace("ё", "е")
    return _SPACE.sub(" ", _PUNCT.sub(" ", t)).strip()


def norm_employer(name: str | None) -> str:
    return _norm(_LEGAL.sub(" ", (name or "").lower()))


def norm_title(name: str | None) -> str:
    return _norm(_PARENS.sub(" ", name or ""))


def dedup_key(v: Vacancy) -> tuple[str, str] | None:
    """Ключ склейки или None, если вакансия в дедупе НЕ участвует.

    Пустой работодатель или тайтл -> None: у анонимных публикаций ключ выродился бы
    в ('', 'python разработчик') и склеил бы вакансии разных компаний."""
    emp, title = norm_employer(v.employer), norm_title(v.name)
    return (emp, title) if emp and title else None


class _HasVacancy(Protocol):
    @property
    def vacancy(self) -> Vacancy: ...


R = TypeVar("R", bound=_HasVacancy)


def dedup_cross_source(records: Sequence[R],
                       priority: Sequence[str]) -> tuple[list[R], Counter[str]]:
    """Убрать копии вакансии, пришедшие с РАЗНЫХ порталов. Возвращает (оставшиеся, сколько
    отброшено по источникам).

    Дедуп ТОЛЬКО межпортальный: если один портал сам отдал вакансию дважды (hh
    переопубликовывает объявления), обе записи остаются — это его собственная история
    публикаций, её разбирает republish_gap, а не мы.

    Победитель группы — источник, который РАНЬШЕ в `priority` (config.SOURCES). Порядок
    там уже задан осмысленно: hh первый, потому что с него работает автоотклик, и терять
    надо копию на портале, куда откликнуться нельзя. Неизвестный источник — в конец.

    Порядок записей сохраняется: вызывающий сохраняет их в кеш как есть."""
    rank = {name: i for i, name in enumerate(priority)}
    last = len(rank)

    winner: dict[tuple[str, str], int] = {}
    for r in records:
        k = dedup_key(r.vacancy)
        if k is None:
            continue
        pos = rank.get(r.vacancy.source, last)
        if k not in winner or pos < winner[k]:
            winner[k] = pos

    kept: list[R] = []
    dropped: Counter[str] = Counter()
    for r in records:
        k = dedup_key(r.vacancy)
        if k is not None and rank.get(r.vacancy.source, last) > winner[k]:
            dropped[r.vacancy.source] += 1
            continue
        kept.append(r)
    return kept, dropped
