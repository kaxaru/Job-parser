"""Value Object уровня опыта. Единый источник маппинга грейдов hirify -> опыт HH
(раньше — hirify._GRADE_TO_EXP/_GRADE_ORDER) и подписи (config.EXP_LABELS)."""
from enum import Enum
from typing import Any

from hrwork.config import EXP_LABELS


class Experience(Enum):
    NONE = "noExperience"       # hh_id = значение enum (совместимо с raw-схемой)
    BETWEEN_1_3 = "between1And3"
    BETWEEN_3_6 = "between3And6"
    MORE_6 = "moreThan6"

    @property
    def hh_id(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return EXP_LABELS[self.value]        # единый источник подписей

    @classmethod
    def from_code(cls, code: str | None) -> "Experience | None":
        """Ленивый парсер сохранённого experience.id / HH workExperience -> уровень.
        Пусто/неизвестный код -> None (опыт не указан). Единый контракт с Schedule.from_code:
        парсеры внешних данных возвращают VO|None, дефолт — на вызывающем."""
        try:
            return cls(code) if code else None
        except ValueError:
            return None

    @classmethod
    def from_hirify_grades(cls, grades: list[dict[str, Any]] | None) -> "Experience | None":
        """Самый МЛАДШИЙ грейд вакансии hirify -> уровень опыта (None, если грейдов нет)."""
        names = {g.get("name") for g in (grades or [])}
        for grade, exp in _GRADE_TO_EXP:        # порядок = от младшего к старшему
            if grade in names:
                return exp
        return None

    @classmethod
    def from_getmatch(cls, seniority: str | None,
                      years: int | None = None) -> "Experience | None":
        """Грейд getmatch (`seniority`) -> уровень опыта; при отсутствии — по числу лет
        (`required_years_of_experience`).

        Приоритет у ГРЕЙДА, а не у лет, хотя лет-поле точнее выглядит: словарь грейдов
        у getmatch тот же, что у hirify (trainee/junior/middle/senior/lead), и один и тот
        же «senior» с обоих порталов обязан попасть в одну корзину — иначе отбор
        кандидатов и срезы аналитики поедут между источниками. Замер 01.08.2026 на 14
        карточках: senior встречался с years=3, что по годам дало бы BETWEEN_1_3."""
        s = (seniority or "").strip().lower()
        for grade, exp in _GRADE_TO_EXP:
            if grade == s:
                return exp
        if years is None:
            return None
        if years <= 0:                          # границы совпадают с вилками HH
            return cls.NONE
        if years <= 3:
            return cls.BETWEEN_1_3
        if years <= 6:
            return cls.BETWEEN_3_6
        return cls.MORE_6


# грейд hirify -> Experience, от младшего к старшему (trainee/junior/middle/senior/lead)
_GRADE_TO_EXP: list[tuple[str, Experience]] = [
    ("trainee", Experience.NONE),
    ("junior",  Experience.BETWEEN_1_3),
    ("middle",  Experience.BETWEEN_1_3),
    ("senior",  Experience.BETWEEN_3_6),
    ("lead",    Experience.MORE_6),
]
