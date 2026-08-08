"""Доменная модель (Model-слой)."""
from dataclasses import dataclass, field

from hrwork.domain import freshness
from hrwork.domain.experience import Experience
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule

# Подпись города, когда привязки к месту НЕТ: полностью удалённая вакансия либо портал
# не назвал локацию. ОДНО значение на все источники — это ключ бакета в фасете городов
# ленты. Пока каждый адаптер писал своё, himalayas отдавал «Worldwide» против «Remote»
# у остальных семи, и одно и то же состояние делилось на два бакета: 11 978 против 219
# (замер 07.08.2026). Смысловой оттенок «нанимают откуда угодно» этим теряется, но он
# и не был виден пользователю — в карточке это просто подпись города.
REMOTE_CITY = "Remote"


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

    def is_remote_like(self) -> bool:
        """Удалёнка ИЛИ гибрид — то, что в срезах, графиках и фильтре ленты называется
        «удалённо» (`Schedule.is_remote_like`, единственный источник ответа).

        Отличать от `is_remote()`: тот отвечает на вопрос «формат РОВНО remote» и нужен
        там, где гибрид не подходит. Расписания нет (портал не назвал формат и вызывающий
        не поставил дефолт) -> НЕ remote-like: домыслить удалёнку из пустого поля значило бы
        завысить срез, а вакансия ушла бы в удалённый фильтр отбора под отклик."""
        sched = self.schedule
        return sched is not None and sched.is_remote_like
