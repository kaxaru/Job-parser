"""Статус свипа формы-анкеты (VO). Раньше — магические строки "ok"/"empty"/"error",
размазанные по forms.py/feed.py/тестам (аудит 2026-07-22): та же болезнь, что была со
`status == "applied"` до `ApplyOutcome`. Enum владеет и кодом хранения, и правилом «мёртвая»."""
from __future__ import annotations

import datetime
from enum import Enum

# Метки в кеше свипа пишутся локальным временем машины; наивную читаем в её же зоне,
# а не в UTC — иначе сравнение с датой публикации уезжает на несколько часов.
_LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo


class FormSweepStatus(Enum):
    OK = "ok"          # поля сняты — форма живая
    EMPTY = "empty"    # свип не нашёл полей — вакансия, скорее всего, снята с публикации
    ERROR = "error"    # страница не открылась / чужой origin

    @property
    def code(self) -> str:
        """Строка для forms_cache.json — схема хранения не меняется."""
        return self.value

    @property
    def is_dead(self) -> bool:
        """Протухшая форма: кандидат на чистку очереди и серую карточку в ленте."""
        return self is not FormSweepStatus.OK

    @classmethod
    def from_code(cls, code) -> FormSweepStatus | None:
        """Мягкий контракт (кеш — внешние данные): неизвестное/отсутствующее -> None."""
        try:
            return cls(code)
        except ValueError:
            return None


def _parse(ts) -> datetime.datetime | None:
    """ISO-строка -> aware datetime (или None). Наивную метку читаем в локальной зоне."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_LOCAL_TZ)


def is_revived(cache_rec: dict | None, published_at) -> bool:
    """Вакансия ПЕРЕОПУБЛИКОВАНА после того, как свип признал её форму мёртвой.

    Мёртвая форма = вакансия снята с публикации, дёргать её бессмысленно. Но HH позволяет
    переоткрыть ту же вакансию (это и есть гост-переопубликация), и тогда форма оживает.
    Сравниваем дату публикации с меткой свипа: свежее -> вакансию можно пробовать снова."""
    swept = _parse((cache_rec or {}).get("ts"))
    pub = _parse(published_at)
    if swept is None or pub is None:
        return False                       # нет одной из дат — считаем, что не оживала
    return pub > swept


def skippable_form_ids(queue, cache: dict, published_by_id: dict) -> set[str]:
    """Какие вакансии-опросники ПРОПУСКАТЬ при отборе кандидатов.

    Пропускаем: живые анкеты (бот их не заполняет — вопросы работодателя специфичны) и
    мёртвые, которые с тех пор не переоткрывали. Переоткрытые из пропуска ВЫХОДЯТ: форма
    вместе с вакансией могла ожить, и это единственный способ вернуть её в оборот, не
    дожидаясь ручной чистки очереди."""
    out: set[str] = set()
    for vid in queue:
        rec = cache.get(str(vid))
        status = FormSweepStatus.from_code((rec or {}).get("status"))
        if status is not None and status.is_dead and is_revived(rec, published_by_id.get(str(vid))):
            continue                        # переоткрыта после смерти формы — пробуем снова
        out.add(str(vid))
    return out
