"""Статус свипа формы-анкеты (VO). Раньше — магические строки "ok"/"empty"/"error",
размазанные по forms.py/feed.py/тестам (аудит 2026-07-22): та же болезнь, что была со
`status == "applied"` до `ApplyOutcome`. Enum владеет и кодом хранения, и правилом «мёртвая»."""
from __future__ import annotations

from enum import Enum


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
