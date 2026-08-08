"""Value Object зарплаты. Единый источник gross->net (НДФЛ 13%) и ПЕРИОДА вилки.

Конвертации в RUB здесь НЕТ и метода `Salary.to_rub` не существует: курсы — внешние данные,
их тянет `infrastructure/net/rates.py::to_rub`, домен в сеть не ходит. До 08.08.2026 эта
шапка обещала конвертацию в домене, и по фантомному символу `Salary.to_rub` ходили ссылки
из `web3career.py` и `docs/collect.md`. Путь конверсии при этом ОДИН — `rates.get_rates()`;
своих курсов не держат ни JS, ни адаптеры, так что это был дефект документации, а не
корректности.

ВСЯ вилка в домене — МЕСЯЧНАЯ. Это инвариант: по ней считаются медианы, срезы аналитики и
сортировка ленты, и годовые 130 000 USD рядом с месячными рублями ломают всё сразу.
Приведение живёт здесь, а не в адаптерах: до 07.08.2026 его дублировали hirify (`_to_monthly`
+ `_infer_period`), himalayas и web3.career, причём порог «это годовая?» разошёлся —
$25 000 у hirify против 15 000 у web3. Один и тот же вопрос обязан иметь один ответ."""
from dataclasses import dataclass
from enum import Enum
from typing import Any

# КОМПРОМИСС (08.08.2026): домен зависит от `hrwork.config`, хотя doc обещает ядро без
# сети и диска. Отсюда берутся ЧИСТЫЕ числовые константы пересчёта — NET_FROM_GROSS (0.87),
# WORK_HOURS_PER_MONTH (160), MONTHS_PER_YEAR (12) и пороги инференса периода
# HIRIFY_HOURLY_MAX_USD / HIRIFY_YEARLY_MIN_USD. Но сам `config` на ИМПОРТЕ делает
# DATA_DIR.mkdir() / LOGS_DIR.mkdir(), load_dotenv, log.remove()+log.add() и читает
# resume_profile.json, поэтому `import hrwork.domain.salary` в чистом окружении создаёт
# data/ и logs/ и переконфигурирует глобальный loguru.
# Держим осознанно: альтернатива — вторая копия тех же чисел в домене, а разошедшийся порог
# «это годовая?» ($25 000 у hirify против 15 000 у web3) — ровно то, ради чего этот модуль
# и заводили. Правильное лечение — вынести чистые константы в модуль без side-effect'ов
# (`config.py` правит только координатор), см. docs/domain.md «Известные компромиссы».
from hrwork.config import (
    HIRIFY_HOURLY_MAX_USD,
    HIRIFY_YEARLY_MIN_USD,
    MONTHS_PER_YEAR,
    NET_FROM_GROSS,
    WORK_HOURS_PER_MONTH,
)


class SalaryPeriod(Enum):
    """За какой срок названа сумма. Доменное понятие: набор конечен, и от него зависит
    пересчёт в месячную — примитивная строка «year»/«hour» здесь была бы тем же дефектом,
    что `tier: int` до появления `ApplyTier`."""
    HOUR = "hour"
    MONTH = "month"
    YEAR = "year"

    @property
    def code(self) -> str:
        return self.value

    @classmethod
    def from_code(cls, code: str | None) -> "SalaryPeriod | None":
        """Ленивый парсер ВНЕШНЕГО значения (`salaryPeriod` himalayas, `salary_unit`
        web3.career и т.п.) -> период. Пусто/незнакомое -> None; выдумывать нельзя:
        ошибка в периоде — это ошибка в 12 раз, хуже пустого поля.

        Единый контракт с Experience.from_code / Schedule.from_code: парсер внешних данных
        возвращает VO|None, дефолт ставит вызывающий."""
        raw = str(code or "").strip().lower()
        if not raw:
            return None
        for period, words in _PERIOD_WORDS:
            if raw in words:
                return period
        return None

    @classmethod
    def infer(cls, usd_mid: float | None) -> "SalaryPeriod":
        """Период ПО ВЕЛИЧИНЕ, когда портал его не назвал. Эвристика, и она осознанная:
        у hirify периода нет в схеме вовсе, у web3.career `salary_unit` заполнен примерно
        у 5 % записей — честно отбрасывать такие вилки значило бы терять почти все.

        Пороги в config (HIRIFY_* исторически названы по первому потребителю): <$300 — час,
        >$25 000 — год, между — месяц."""
        v = usd_mid or 0
        if 0 < v < HIRIFY_HOURLY_MAX_USD:
            return cls.HOUR
        if v > HIRIFY_YEARLY_MIN_USD:
            return cls.YEAR
        return cls.MONTH

    def to_monthly(self, amount: float | None) -> int | None:
        """Сумму в ИСХОДНОЙ валюте -> месячная: час×160 (раб.часов/мес), год/12, месяц как есть."""
        if amount is None:
            return None
        try:
            v = float(amount)
        except (TypeError, ValueError):
            return None
        if self is SalaryPeriod.HOUR:
            return round(v * WORK_HOURS_PER_MONTH)
        if self is SalaryPeriod.YEAR:
            return round(v / MONTHS_PER_YEAR)
        return round(v)


# Диалекты порталов -> период. himalayas: annual/monthly/hourly; web3.career: YEAR/MONTH/HOUR;
# hirify оперирует своими year/month/hour. Свести в одну таблицу дешевле, чем в каждом
# адаптере писать свой разбор.
_PERIOD_WORDS: list[tuple[SalaryPeriod, frozenset[str]]] = [
    (SalaryPeriod.HOUR,  frozenset({"hour", "hourly", "per_hour", "час"})),
    (SalaryPeriod.MONTH, frozenset({"month", "monthly", "per_month", "мес", "месяц"})),
    (SalaryPeriod.YEAR,  frozenset({"year", "yearly", "annual", "annually", "per_year", "год"})),
]


@dataclass(frozen=True)
class Salary:
    frm: int | None
    to: int | None
    currency: str | None = None
    gross: bool = False

    @classmethod
    def from_raw(cls, sal: dict[str, Any] | None) -> "Salary | None":
        """{from,to,currency,gross} -> Salary (или None, если вилки нет)."""
        if not sal or (sal.get("from") is None and sal.get("to") is None):
            return None                          # is None, не truthiness: вилка from=0 — валидна
        return cls(sal.get("from"), sal.get("to"), sal.get("currency"), bool(sal.get("gross")))

    @classmethod
    def monthly(cls, frm: Any, to: Any, currency: Any = None,
                period: "SalaryPeriod | None" = None, *, gross: bool = False) -> "Salary | None":
        """Вилка ИЗВЕСТНОГО периода -> месячная (доменный инвариант, см. шапку модуля).

        `period=None` -> None: вилку НЕ берём. Домен не решает за адаптер, можно ли угадать
        период, потому что это знание о качестве данных КОНКРЕТНОГО портала:
          * himalayas отдаёт `salaryPeriod` практически везде — неизвестное значение там
            аномалия, и брать её опасно (ошибка в периоде это ошибка в 12 раз);
          * у web3.career поле заполнено у ~5 %, у hirify его нет вовсе — там отбросить
            значило бы потерять почти все вилки, и инференс оправдан.
        Кому нужен инференс — передаёт `SalaryPeriod.infer(...)` ЯВНО, и это видно в
        месте вызова.

        Валюта сохраняется как есть: конвертацию делает `to_rub`, а подставлять валюту по
        умолчанию нельзя — на крипто-рынке платят и в стейблкоинах.
        Пустая вилка -> None, как контракт `from_raw`."""
        if period is None or (frm is None and to is None):
            return None
        f, t = period.to_monthly(frm), period.to_monthly(to)
        if f is None and t is None:
            return None
        return cls(f, t, currency, gross=gross)

    @property
    def mid(self) -> int | None:
        if self.frm is not None and self.to is not None:
            return (self.frm + self.to) // 2
        return self.frm if self.frm is not None else self.to

    def net(self) -> "Salary":
        """gross -> net (−13% НДФЛ). Если уже net — возвращает себя.

        `is not None`, а не truthiness: вилка from=0 ВАЛИДНА (тот же контракт, что у
        `from_raw`). БАГ до 08.08.2026: `if self.frm` терял нулевую нижнюю границу, и
        {"from": 0, "to": 100000, "gross": true} давал (None, 87000, 87000) — медиана
        87 000 вместо 43 500.

        `round`, а не `int`: усечение давало 4166 там, где верно 4167. Разница копеечная
        (<= 1 руб.), но симметрия с `SalaryPeriod.to_monthly`, который уже округляет,
        важнее — один и тот же пересчёт обязан вести себя одинаково."""
        if not self.gross:
            return self
        f = round(self.frm * NET_FROM_GROSS) if self.frm is not None else None
        t = round(self.to * NET_FROM_GROSS) if self.to is not None else None
        return Salary(f, t, self.currency, gross=False)

    def net_triple(self) -> tuple[int | None, int | None, int | None]:
        """(from, to, mid) уже net — для заполнения примитивных полей Vacancy (совместимость)."""
        n = self.net()
        return n.frm, n.to, n.mid
