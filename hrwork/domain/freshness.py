"""Свежесть вакансии по таймингу HH: возраст, разрыв «создана↔переоткрыта», класс.

Сигнал (только из поисковой выдачи): creationTime (реальное создание) vs
publicationTime (последнее поднятие). Гост-вакансия — старая по creationTime, но
регулярно переопубликованная (висит месяцами). Свежая — создана в последний месяц.

Чистая логика без сети/БД (тестируется напрямую). «Сегодня» инжектируется, чтобы
тесты были детерминированы, а дашборд/лента считали от даты сборки.
"""
from __future__ import annotations

import datetime
from enum import Enum
from typing import Any

FRESH_DAYS = 30     # <=30 дней от создания — свежая
GHOST_DAYS = 60     # >60 дней и всё ещё висит — гост-вакансия


class FreshnessClass(Enum):
    """Класс свежести вакансии — ЕДИНЫЙ источник кода/подписи/цвета/порядка.

    Раньше это были три параллельных словаря (FRESHNESS_LABELS/ORDER здесь + FRESHNESS_COLORS
    в config), синхронные вручную и БЕЗ страж-теста: цветовой словарь молча не имел ключа
    `unknown` (латентный KeyError), а код `"fresh"/"ghost"/…` хардкодился ~15× по слоям.
    Enum убирает рассинхрон by construction: нет класса без подписи, `unknown` без цвета —
    явный None, а не пропуск.

    `.code` (= value) — строка на диск/в JS-ленту (view.js: `v.fresh === 'ghost'`), граница
    не меняется. Цвет — презентационный, но живёт здесь ради единого источника; CVD-выбор
    (синий, не зелёный) обоснован ниже у _COLORS.
    """
    FRESH = "fresh"       # <=30 дней от создания
    RECENT = "recent"     # 30–60 дней
    GHOST = "ghost"       # >60 дней и всё ещё висит
    UNKNOWN = "unknown"   # даты нет

    @property
    def code(self) -> str:
        """Строковый код для диска/JS/CSV (тот же, что был литералом)."""
        return self.value

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def color(self) -> str | None:
        """Цвет марки на дашборде (None у UNKNOWN — не рисуется)."""
        return _COLORS.get(self)

    @property
    def order(self) -> int:
        """Порядок шкалы свежести (fresh -> unknown) — для стека графиков/CSV."""
        return _ORDER[self]

    @classmethod
    def from_code(cls, code: str) -> FreshnessClass:
        """Строгий roundtrip доверенного wire-кода (fresh/recent/ghost/unknown) -> класс.
        Raise на незнакомом — инверсия .code, не парсер внешних данных."""
        return cls(code)

    @classmethod
    def dated(cls) -> tuple[FreshnessClass, ...]:
        """Классы с реальной датой (без UNKNOWN) — база для долей/ghost_pct."""
        return (cls.FRESH, cls.RECENT, cls.GHOST)


_LABELS = {
    FreshnessClass.FRESH:   "Свежие (≤30 дн)",
    FreshnessClass.RECENT:  "30–60 дн",
    FreshnessClass.GHOST:   "Гост (>60 дн)",
    FreshnessClass.UNKNOWN: "Без даты",
}
# Палитра свежести. Единый источник для всех графиков дашборда: один и тот же смысл обязан
# выглядеть одинаково в любой вкладке. Синий, а НЕ зелёный, для «свежих»: зелёный #54A24B с
# красным #E45756 при дейтеранопии дают ΔE 1.2 — для дальтоника это один цвет, светофор
# нечитаем. Синий↔красный различимы. Тройка проверена валидатором на подложке BG:
# яркость/хрома/контраст — PASS, CVD 7.7 (диапазон 6–8 допустим ТОЛЬКО при вторичном
# кодировании -> легенда + подписи + зазор).
_COLORS = {
    FreshnessClass.FRESH:  "#4C9BD1",
    FreshnessClass.RECENT: "#BB8B31",
    FreshnessClass.GHOST:  "#D64550",
}
_ORDER = {fc: i for i, fc in enumerate(FreshnessClass)}


def parse_dt(value: Any) -> datetime.datetime | None:
    """ISO-строка ('2026-04-30T08:20:03+03:00') или unix-секунды -> aware datetime (UTC)."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)
        s = str(value).strip()
        if s.isdigit():
            return datetime.datetime.fromtimestamp(int(s), tz=datetime.timezone.utc)
        if s.endswith("Z"):                     # hirify: '...T07:26:02.000000Z' (3.10 не ест 'Z')
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _days_between(later: datetime.datetime, earlier: datetime.datetime | None) -> int | None:
    if earlier is None:
        return None
    return (later - earlier).days


def age_days(created_iso: Any, today: datetime.datetime | None = None) -> int | None:
    """Сколько дней вакансии с момента СОЗДАНИЯ (реальный возраст)."""
    now = today or datetime.datetime.now(tz=datetime.timezone.utc)
    return _days_between(now, parse_dt(created_iso))


def republish_gap_days(created_iso: Any, published_iso: Any) -> int | None:
    """Разрыв между созданием и последней публикацией: >0 = вакансию переоткрывали."""
    created, published = parse_dt(created_iso), parse_dt(published_iso)
    if created is None or published is None:
        return None
    return max(0, (published - created).days)


def classify_age(age: int | float | None) -> FreshnessClass:
    """FreshnessClass по ГОТОВОМУ возрасту в днях.
    Отдельно от classify: возраст бывает уже посчитан и агрегирован (медиана по компании),
    исходной даты там нет. Пороги — один источник для обоих входов."""
    if age is None:
        return FreshnessClass.UNKNOWN
    if age <= FRESH_DAYS:
        return FreshnessClass.FRESH
    if age <= GHOST_DAYS:
        return FreshnessClass.RECENT
    return FreshnessClass.GHOST


def classify(created_iso: Any, today: datetime.datetime | None = None) -> FreshnessClass:
    """FreshnessClass по возрасту от СОЗДАНИЯ."""
    return classify_age(age_days(created_iso, today))


def is_ghost(created_iso: Any, today: datetime.datetime | None = None) -> bool:
    return classify(created_iso, today) is FreshnessClass.GHOST
