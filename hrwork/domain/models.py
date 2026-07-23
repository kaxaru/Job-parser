"""Доменная модель (Model-слой)."""
from dataclasses import dataclass, field

from hrwork.domain import freshness
from hrwork.domain.experience import Experience
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule


@dataclass
class Vacancy:
    id: str
    name: str
    city: str
    city_id: str
    salary: Salary | None       # net зарплата (gross→net внутри VO); None, если вилки нет
    experience: Experience | None    # уровень опыта; None — не указан
    schedule: Schedule               # формат работы (remote/flexible/fullDay)
    techs: list[str] = field(default_factory=list)
    role: Role = Role.NON_IT    # роль по тайтлу (VO; Backend/QA/…/NON_IT) — для фильтра
    employer: str = ''          # название компании (для агрегации по работодателям)
    created_at: str | None = None    # ISO: когда вакансия создана (реальный возраст)
    published_at: str | None = None  # ISO: когда последний раз поднята/переопубликована
    responses: int | None = None     # сколько уже откликнулось (конкуренция)
    source: str = 'hh'               # портал-источник (hh / hirify / …) — для агрегатора

    # ── Поведение сущности над её же датами/форматом (раньше — свободные функции
    # freshness.*(v.created_at) у каждого потребителя). Тонкие делегаты к доменному
    # сервису freshness: единый язык «спроси у вакансии», а не «прогони дату через модуль».
    def age_days(self) -> int | None:
        """Возраст в днях с момента создания (None, если даты нет)."""
        return freshness.age_days(self.created_at)

    def fresh_class(self) -> freshness.FreshnessClass:
        """Класс свежести (VO): FRESH / RECENT / GHOST / UNKNOWN."""
        return freshness.classify(self.created_at)

    def is_ghost(self) -> bool:
        """Висит >60 дней от создания — отклик почти бессмыслен."""
        return freshness.is_ghost(self.created_at)

    def republish_gap_days(self) -> int | None:
        """Разрыв создание↔последняя публикация: >0 = вакансию переоткрывали."""
        return freshness.republish_gap_days(self.created_at, self.published_at)

    def is_remote(self) -> bool:
        """Формат помечен как удалёнка (строго schedule=remote; гибрид — отдельно).
        Текстовые маркеры удалёнки в описании — не здесь: их нет на сущности."""
        return self.schedule is Schedule.REMOTE
