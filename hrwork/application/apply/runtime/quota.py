"""Дневная квота откликов (HH режет на ~200/сутки), идемпотентно между запусками дня.

Счётчик в apply_quota.json ({"date","count"}); запись за прошлый день = 0. Вынесено из
autoclick — чистая логика над файлом, без браузера, тестируется напрямую."""
import datetime
from threading import Lock
from typing import Any

from hrwork.config import DATA_DIR, HH_DAILY_APPLY_CAP
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

QUOTA_FILE = DATA_DIR / "apply_quota.json"       # {"date": "YYYY-MM-DD", "count": N}
DAILY_CAP_DEFAULT = HH_DAILY_APPLY_CAP           # потолок откликов в сутки (лимит HH, config)
_WRITE_LOCK = Lock()   # read-modify-write не рвётся между потоками одного процесса


def _today() -> str:
    return datetime.date.today().isoformat()


def _load_quota() -> dict[str, Any]:
    data: dict[str, Any] = read_json_or(QUOTA_FILE, {})
    return data


def applied_today(quota: dict[str, Any] | None = None) -> int:
    """Сколько откликов уже сделано СЕГОДНЯ (0, если запись за прошлый день)."""
    q = quota if quota is not None else _load_quota()
    return int(q.get("count", 0)) if q.get("date") == _today() else 0


def bump_quota(n: int) -> int:
    """Прибавить n к сегодняшнему счётчику (атомарно). Возвращает новый итог.

    Межпроцессной защиты у read-modify-write нет и не будет: писателей разводит
    `lock.py::_single_instance` (с 08.08.2026 он берётся через O_EXCL, то есть ровно один
    процесс держит браузер). Единственный писатель ВНЕ lock'а — `reconcile_quota` из синка,
    и он монотонный, поэтому потерянный инкремент там самовосстанавливается на следующем
    выравнивании, а не копится."""
    if n <= 0:
        return applied_today()
    with _WRITE_LOCK:
        total = applied_today() + n
        atomic_write_json(QUOTA_FILE, {"date": _today(), "count": total})
    return total


def reconcile_quota(journaled_today: int) -> int:
    """Выровнять сегодняшний счётчик по ФАКТУ (число откликов в журнале за сегодня).
    Возвращает итог после выравнивания.

    Зачем: окно «клик -> учёт» не покрыто ничем. `_apply_batch` жмёт кнопку, потом
    `mark_applied` -> `bump_quota` -> `log_applied`; watchdog между кликом и `bump_quota`
    (или подтверждение HH на 11-й секунде, когда `apply_one` уже вернул SKIP) означает, что
    отклик УШЁЛ, а счётчик его не увидел. Ни `sync_statuses`, ни `_sync_applied_from_chats`
    квоту не трогали, поэтому недосчёт жил до полуночи и бот слал cap+N за день.

    ТОЛЬКО ВВЕРХ. Уменьшать нельзя: журнал догоняет реальность с задержкой (запись идёт
    после инкремента, а ручные отклики попадают в него лишь после синка чатов), и «выравнивание»
    вниз открыло бы дорогу к превышению лимита HH — то есть ровно к тому, от чего защищает
    квота. Повтор безопасен: функция идемпотентна (второй вызов с тем же фактом не меняет
    ничего). Наблюдаемость — строка «Квота расходится с журналом» в логе прогона."""
    with _WRITE_LOCK:
        current = applied_today()
        if journaled_today <= current:
            return current
        atomic_write_json(QUOTA_FILE, {"date": _today(), "count": journaled_today})
    return journaled_today
