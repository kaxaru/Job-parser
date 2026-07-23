"""Value Object формата работы. Единый источник маппинга HH/hirify -> домен и обратно
(раньше логика дублировалась: html_client._schedule_from_formats + hirify._schedule_from_wf)."""
from enum import Enum


class Schedule(Enum):
    REMOTE = "remote"       # hh_code = значение enum (совместимо с raw-схемой и лентой)
    HYBRID = "flexible"
    OFFICE = "fullDay"

    @property
    def hh_code(self) -> str:
        """Код для raw-схемы/ленты (schedule.id): remote/flexible/fullDay."""
        return self.value

    @property
    def label(self) -> str:
        return {"remote": "Удалённо", "flexible": "Гибрид", "fullDay": "Офис"}[self.value]

    @classmethod
    def from_code(cls, code: str | None) -> "Schedule | None":
        """Ленивый парсер сохранённого schedule.id (remote/flexible/fullDay) -> формат.
        Пусто/неизвестный код -> None; доменный дефолт (OFFICE) применяет ВЫЗЫВАЮЩИЙ.
        Единый контракт с Experience.from_code: парсеры внешних данных возвращают VO|None и
        НЕ выдумывают значение. (Строгий roundtrip доверенного кода — Role.from_label /
        FreshnessClass.from_code, они кидают ValueError на незнакомом входе.)"""
        try:
            return cls(code) if code else None
        except ValueError:
            return None

    @classmethod
    def from_hh_formats(cls, formats: list[str]) -> "Schedule":
        """workFormats HH (ON_SITE/HYBRID/REMOTE) -> формат. Приоритет remote > hybrid > офис."""
        if "REMOTE" in (formats or []):
            return cls.REMOTE
        if "HYBRID" in (formats or []):
            return cls.HYBRID
        return cls.OFFICE

    @classmethod
    def from_hirify_wf(cls, work_format: list[str]) -> "Schedule":
        """work_format hirify (remote/hybrid/onsite) -> формат. Тот же приоритет remote > hybrid."""
        wf = work_format or []
        if "remote" in wf:
            return cls.REMOTE
        if "hybrid" in wf:
            return cls.HYBRID
        return cls.OFFICE
