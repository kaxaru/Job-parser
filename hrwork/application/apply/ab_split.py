"""Владение пулом двух аккаунтов (RFC-004): основной не берёт ничего из ниши acc2.

Второй аккаунт (`acc2`) заведён под свою нишу (Python backend, удалёнка+гибрид) и имеет
ПРИОРИТЕТ на неё. Модель (решение владельца 16.09.2026, заменяет прежний A/B-50/50 по хешу):

- **acc2 берёт весь свой пул** — все вакансии, подходящие по его правилам, включая те, на
  которые основной откликался раньше. Свою историю acc2 не дублит (`taken.own_handled_ids`,
  RFC-004: R26), поэтому `reject` для него всегда None (R24).
- **Основной ОТСТУПАЕТ** от пула acc2: его отбор вычитает ВЕСЬ `eligible.json` acc2 целиком
  (не половину). Гарантия ОДНОСТОРОННЯЯ: основной не возьмёт вакансию из пула acc2, а acc2
  свою нишу добирает и поверх его откликов — один работодатель может получить два отклика
  одного человека с двух резюме (замер 21.09.2026: 72 из 103 откликов acc2 — на вакансии из
  журнала основного). Конфликт виден в ленте и в WARNING синка; причина — RFC-004, врезка
  «ЗАМЕНЕНО 16.09.2026 (S8)».
- Отступление основного НЕ зависит от того, жива ли сессия acc2 (R25): приоритет за acc2, даже
  когда он в дауне (вход по SMS ~раз в 2 недели). Цена — ниша acc2 простаивает, пока acc2 без
  входа.

**Общая часть (`eligible.json`)** — вакансии, подходящие по правилам acc2. Её считает только
acc2 (правила пекутся на импорте, основной их не знает) и освежает в начале своих прогонов и
браузерно-независимо после сбора (`cron_collect.bat` -> `--dry-pool --write-eligible`). Набор
зависит только от кеша вакансий и правил, но не от marks и журнала.

- `eligible.json` устарел (собран по другому срезу кеша) — основной всё равно вычитает
  последний известный набор: недобрать безопаснее, чем пустить дубль. В лог — WARNING.
- `eligible.json` ещё нет (acc2 ни разу не считал) — сплита нет, основной берёт весь пул.

С одним аккаунтом сплита нет — отбор основного не меняется.
"""
import datetime
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from hrwork.application.apply import taken
from hrwork.config import ACCOUNT, log
from hrwork.domain.account import MAIN_CODE
from hrwork.infrastructure.storage import atomic_write_json, collected_at, read_json_or

ELIGIBLE_FILE_NAME = "eligible.json"   # {"built_at": ISO, "cache_collected_at": float|None, "ids": [...]}
REASON_FOREIGN = "A/B: доля другого аккаунта"


class AbSplitError(RuntimeError):
    """Сплит не определён — заведено больше двух аккаунтов."""


def second_account_code() -> str | None:
    """Код второго аккаунта; None — аккаунт один. Больше двух — сплит не определён."""
    others = [code for code in taken.account_dirs() if code != MAIN_CODE]
    if len(others) > 1:
        raise AbSplitError(f"A/B-сплит рассчитан на два аккаунта, заведены: main, {', '.join(others)}")
    return others[0] if others else None


def eligible_path(code: str) -> Path:
    return taken.account_dirs()[code] / ELIGIBLE_FILE_NAME


def write_eligible(ids: Iterable[str]) -> Path:
    """Записать пул текущего (второго) аккаунта со штампом среза кеша."""
    if ACCOUNT.is_main:
        raise AbSplitError("пул acc2 пишет только второй аккаунт — у основного свои правила")
    path = eligible_path(ACCOUNT.code)
    unique = sorted(set(map(str, ids)))
    atomic_write_json(path, {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cache_collected_at": collected_at(),
        "ids": unique,
    })
    log.info("A/B: пул {} — {} вакансий ({})", ACCOUNT.code, len(unique), path.name)
    return path


@dataclass(frozen=True)
class AbSplit:
    codes: tuple[str, str]           # (main, второй)
    me: str
    eligible: frozenset[str]         # пул acc2 по его файлу
    eligible_fresh: bool             # собран по текущему срезу кеша (для WARNING/аналитики)

    def reject(self, vid: str) -> str | None:
        """Причина не брать вакансию в пул текущего аккаунта, либо None. acc2 — приоритет, берёт
        весь свой пул (None); основной вычитает ВЕСЬ пул acc2 (`vid in eligible`)."""
        if self.me != MAIN_CODE:
            return None
        return REASON_FOREIGN if vid in self.eligible else None

    def in_common(self, vid: str) -> bool:
        """Была ли вакансия в пуле acc2 на момент отклика (поле `ab` журнала)."""
        return self.me != MAIN_CODE or vid in self.eligible


def current_split() -> AbSplit | None:
    """Владение пулом для текущего процесса; None — аккаунт один или у acc2 ещё нет eligible.
    Не зависит от того, жива ли сессия acc2: основной отступает от пула acc2 всегда."""
    second = second_account_code()
    if second is None:
        return None
    data = read_json_or(eligible_path(second), {})
    if not data:
        # acc2 НИ РАЗУ не считал свой пул -> в делёж ещё не вошёл. Сплита нет, основной берёт
        # весь пул. Двойной отклик и здесь исключён `taken.py` (журнал acc2).
        if ACCOUNT.is_main:
            log.info("A/B выключен: у {} ещё нет eligible.json (не запускался) — {} берёт весь пул",
                     second, ACCOUNT.code)
        return None
    ids = frozenset(str(i) for i in (data.get("ids") or []))
    stamp = collected_at()
    fresh = stamp is not None and data.get("cache_collected_at") == stamp
    if ACCOUNT.is_main and not fresh:
        log.warning("A/B: пул {} устарел — вычитаю последний известный набор ({} вакансий)",
                    second, len(ids))
    return AbSplit(codes=(MAIN_CODE, second), me=ACCOUNT.code, eligible=ids, eligible_fresh=fresh)


@dataclass(frozen=True)
class PoolInputs:
    """Входы отбора пула — VO вместо неименованной тройки (аудит 22.09.2026, §5).

    Раньше `pool_inputs` возвращал `(marks, taken, ab_reject)`, и все вызывающие распаковывали
    его ПОЗИЦИОННО: перестановка двух полей в возврате не поймалась бы ни типами, ни тестом
    (обе коллекции строк), а `pick_candidates` разрастался до семи параметров.

    Значения по умолчанию — «ничего не исключаем»: пустые marks, пустой `taken`, без A/B."""
    marks: dict[str, str] = field(default_factory=dict)
    taken: Collection[str] = frozenset()
    ab_reject: Callable[[str], str | None] | None = None


def pool_inputs(split: "AbSplit | None") -> PoolInputs:
    """Входы отбора пула ТЕКУЩЕГО аккаунта — ЕДИНЫЙ источник для боевого `_apply_batch` и
    превью `--dry-pool`, чтобы превью не расходилось с реальным прогоном.

    - Основной: общие `marks.json` + журналы других аккаунтов (`taken_by_others`) + вычитание
      пула acc2 (`split.reject`).
    - Приоритетный (acc2): дедуп ТОЛЬКО против своей истории (`own_handled_ids`), никем не
      блокируется (`taken` пуст), A/B не режет — берёт весь свой пул, включая ground основного.
    """
    from hrwork.application.apply.runtime.store import store  # лениво: избегаем цикла импорта
    if ACCOUNT.is_main:
        return PoolInputs(marks=store.marks(), taken=taken.taken_by_others(),
                          ab_reject=(split.reject if split is not None else None))
    return PoolInputs(marks=taken.own_handled_ids())
