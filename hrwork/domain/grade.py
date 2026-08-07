"""Value Object грейда вакансии — junior / middle / senior.

Отличается от `Experience` намеренно: `Experience` — это ВИЛКА ОПЫТА, которую называет
работодатель (структурное поле портала), а `Grade` — уровень позиции, который читается из
ТАЙТЛА. Связаны, но не одно и то же: «Senior Python» с вилкой «1–3 года» встречается, и там
побеждает тайтл. `.code` = ключ в `resume_profile.json::answers.salary_by_grade`.

Словарь живёт ЗДЕСЬ, а не у потребителей. До 07.08.2026 существовали ДВА независимых
`Grade` — в `chat_answer.py` и в `form_fill.py`, — и их лексиконы разошлись:
  * senior: только у чата были `старш`, `\\bsr\\b`, `\\bstaff\\b`, `принципал`;
    только у анкет — `тимлид`, `principal` (латиницей), `архитектор`;
  * junior: только у чата `\\bjr\\b`, `ученик`; только у анкет `интерн`, `intern`, `trainee`.
Одна и та же вакансия получала разный грейд в зависимости от того, кто спрашивает, а от
грейда зависит НАЗЫВАЕМАЯ РАБОТОДАТЕЛЮ СУММА. Здесь лексиконы объединены.

Чего этот модуль НЕ решает — что делать, когда грейд не определился. Это решает вызывающий,
и решения у него разные по существу (см. `from_vacancy`).
"""
import re
from enum import Enum

from hrwork.domain.experience import Experience


class Grade(Enum):
    """Уровень позиции. Значение = ключ `salary_by_grade` и суффикс rule; wire-формат
    меняться не должен."""
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"

    @property
    def code(self) -> str:
        return self.value

    @classmethod
    def from_title(cls, name: str) -> "Grade | None":
        """Грейд по ТАЙТЛУ вакансии. None — в тайтле его нет (замер: у 74 % вакансий)."""
        for grade, rx in _TITLE_RX:
            if rx.search(name or ""):
                return grade
        return None

    @classmethod
    def from_experience(cls, exp: Experience | None) -> "Grade | None":
        """Грейд по вилке опыта — фолбэк, когда тайтл молчит. Поле `experience` заполнено
        почти всегда, в отличие от тайтла."""
        return _BY_EXP.get(exp) if exp else None

    @classmethod
    def from_vacancy(cls, name: str, experience_code: str | None = None) -> "Grade | None":
        """Грейд вакансии: ТАЙТЛ сильнее структурного поля, затем вилка опыта, иначе None.

        Тайтл в приоритете, потому что «Senior …» при вилке «1–3 года» — всё равно senior.

        None означает «не определить», и дефолт ставит ВЫЗЫВАЮЩИЙ, а не домен: в чате
        промолчать про деньги дешевле, чем назвать вилку наугад, а в анкете поле обязано
        быть заполнено, иначе вакансия уходит человеку. Это разные цены ошибки, и домену
        выбирать за них нечем."""
        by_title = cls.from_title(name)
        if by_title is not None:
            return by_title
        return cls.from_experience(Experience.from_code(experience_code))


# Тайтл -> грейд. ОБЪЕДИНЁННЫЕ лексиконы обеих прежних копий, порядок значим: senior
# проверяется раньше junior, иначе «Senior Java для junior-команды» уехал бы в JUNIOR.
_TITLE_RX: tuple[tuple[Grade, "re.Pattern[str]"], ...] = (
    (Grade.SENIOR, re.compile(
        r"\bsenior\b|сеньор|синьор|старш|ведущ|\blead\b|тимлид|тим-?лид|\bsr\b|\bstaff\b"
        r"|\bprincipal\b|принципал|архитектор|\barchitect\b", re.I)),
    (Grade.MIDDLE, re.compile(r"\bmiddle\b|\bmid\b|мидл|средн", re.I)),
    (Grade.JUNIOR, re.compile(
        r"\bjunior\b|джуниор|младш|стаж[её]р|\bjr\b|ученик|интерн|\bintern|trainee", re.I)),
)
# Вилка опыта -> грейд. «1–3 года» отнесены к JUNIOR — осознанное решение владельца
# профиля (20.07.2026), а не общерыночная шкала.
_BY_EXP: dict[Experience, Grade] = {
    Experience.NONE: Grade.JUNIOR,
    Experience.BETWEEN_1_3: Grade.JUNIOR,
    Experience.BETWEEN_3_6: Grade.MIDDLE,
    Experience.MORE_6: Grade.SENIOR,
}
