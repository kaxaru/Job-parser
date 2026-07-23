"""Value Object уровня опыта. Единый источник маппинга грейдов hirify -> опыт HH
(раньше — hirify._GRADE_TO_EXP/_GRADE_ORDER) и подписи (config.EXP_LABELS)."""
from enum import Enum

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
    def from_hirify_grades(cls, grades: list[dict]) -> "Experience | None":
        """Самый МЛАДШИЙ грейд вакансии hirify -> уровень опыта (None, если грейдов нет)."""
        names = {g.get("name") for g in (grades or [])}
        for grade, exp in _GRADE_TO_EXP:        # порядок = от младшего к старшему
            if grade in names:
                return exp
        return None


# грейд hirify -> Experience, от младшего к старшему (trainee/junior/middle/senior/lead)
_GRADE_TO_EXP: list[tuple[str, Experience]] = [
    ("trainee", Experience.NONE),
    ("junior",  Experience.BETWEEN_1_3),
    ("middle",  Experience.BETWEEN_1_3),
    ("senior",  Experience.BETWEEN_3_6),
    ("lead",    Experience.MORE_6),
]
