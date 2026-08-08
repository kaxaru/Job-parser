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
    def from_getmatch(cls, formats: list[str] | None) -> "Schedule":
        """`format` из location_items getmatch (office/hybrid/remote/relocation_company)
        -> формат. Тот же приоритет remote > hybrid > офис, что у остальных порталов.

        `relocation_company` — это переезд ради работы В ОФИСЕ, поэтому офис, а не удалёнка:
        иначе такие вакансии проходили бы удалённый фильтр отбора под отклик."""
        f = formats or []
        if "remote" in f:
            return cls.REMOTE
        if "hybrid" in f:
            return cls.HYBRID
        return cls.OFFICE

    @classmethod
    def from_hirify_wf(cls, work_format: list[str] | None) -> "Schedule":
        """work_format hirify (remote/hybrid/onsite) -> формат. Тот же приоритет remote > hybrid."""
        wf = work_format or []
        if "remote" in wf:
            return cls.REMOTE
        if "hybrid" in wf:
            return cls.HYBRID
        return cls.OFFICE

    @classmethod
    def from_talanto(cls, raw: str | None) -> "Schedule | None":
        """`remote_type` talanto (remote/hybrid/office) -> формат.

        МЯГКИЙ контракт, как у `from_code`: пусто/незнакомое -> None, доменный дефолт
        (OFFICE) ставит ВЫЗЫВАЮЩИЙ. Регистр и внешние пробелы нормализуются здесь же —
        нормализация внешнего значения это работа парсера, а не каждого адаптера.

        Отличие от `from_hirify_wf`/`from_getmatch` (те возвращают Schedule, а не None) не
        стилистическое: там на входе СПИСОК форматов, и «ни одного из известных» честно
        значит «офис»; здесь на входе одно поле, и пустое поле — это «портал не сказал»,
        а не «офис».

        Словарь живёт ЗДЕСЬ, а не в адаптере: своя копия `_SCHED` в talanto.py — ровно тот
        класс инцидента, что был с грейдами (адаптерная таблица разошлась с доменной, и один
        и тот же ярлык значил на разных порталах разное)."""
        return _TALANTO.get(str(raw or "").strip().lower())

    @property
    def is_remote_like(self) -> bool:
        """«Удалёнкоподобность» = REMOTE + HYBRID. ЕДИНСТВЕННЫЙ ответ на вопрос «это
        удалёнка?» для срезов, графиков и фильтров ленты.

        До 08.08.2026 ответов было два: `analyzer.py::_REMOTE` считал гибрид удалёнкой,
        а кнопка «Офис» в `feed/model.js::filterVacancies` пропускала `flexible` в офисный
        бакет. Расхождение — 16 857 вакансий, одновременно «офис» в ленте и «удалёнка»
        в отчётах 09/11/12 и графиках chart_remote/chart_by_source. Побеждает домен:
        гибрид требует присутствия в офисе не каждый день, и для отбора под отклик это
        ближе к удалёнке, чем к офису."""
        return self in _REMOTE_LIKE


# Коды talanto -> VO. Мягкий парсер: ключа нет -> None (см. `Schedule.from_talanto`).
_TALANTO: dict[str, Schedule] = {
    "remote": Schedule.REMOTE,
    "hybrid": Schedule.HYBRID,
    "office": Schedule.OFFICE,
}

# Что считается удалёнкой. Один кортеж на предикат и на мост в JS — чтобы вторая сторона
# не могла разойтись с первой (инцидент 08.08.2026, см. `Schedule.is_remote_like`).
_REMOTE_LIKE: tuple[Schedule, ...] = (Schedule.REMOTE, Schedule.HYBRID)

# Коды тех же форматов для JS: константа определяется в Python и инжектится в feed-data.js
# (конвенция CLAUDE.md «константа, нужная и Python и JS»). Дублировать список в model.js
# нельзя — так уже разъехались APPLY_CORE и resume.js.
REMOTE_LIKE_CODES: tuple[str, ...] = tuple(s.hh_code for s in _REMOTE_LIKE)
