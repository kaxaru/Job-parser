"""Дневная квота откликов (HH режет на ~200/сутки), идемпотентно между запусками дня.

Счётчик в apply_quota.json ({"date","count"}); запись за прошлый день = 0. Вынесено из
autoclick — чистая логика над файлом, без браузера, тестируется напрямую.

Здесь же — счётчик СКОЛЬЗЯЩИХ 24ч по журналу откликов (`applied_in_window`): второй, не
документированный HH потолок, в который упирается прогон (замер 23.09.2026, см.
`config.HH_APPLY_ROLLING_CAP`). Считается по журналу, а не по apply_quota.json: суточный
счётчик обнуляется в полночь и про вчерашний вечер ничего не знает."""
import datetime
import re
from threading import Lock
from typing import Any

from hrwork.config import ACCOUNT_DIR, HH_DAILY_APPLY_CAP
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

QUOTA_FILE = ACCOUNT_DIR / "apply_quota.json"    # {"date": "YYYY-MM-DD", "count": N}; лимит HH — на аккаунт
DAILY_CAP_DEFAULT = HH_DAILY_APPLY_CAP           # потолок откликов в сутки (лимит HH, config)
ROLLING_WINDOW_H = 24    # длина окна, для которого задан HH_APPLY_ROLLING_CAP
_WRITE_LOCK = Lock()   # read-modify-write не рвётся между потоками одного процесса

# `ts` журнала приходит в двух формах, и обе надо разбирать: наши записи — 6 знаков доли
# секунды и локальное смещение, а дожурналенные синком из чатов — ПЯТЬ знаков и +03:00
# (`2026-09-15T19:15:21.85756+03:00`, время HH). `datetime.fromisoformat` в 3.10 на пяти
# знаках бросает ValueError, поэтому долю секунды нормализуем до шести.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def journal_ts(raw: str) -> datetime.datetime | None:
    """Момент отклика из `ts` строки журнала; None — если запись не разбирается.

    Нет смещения — считаем время локальным (`.astimezone()` на naive-значении), а не UTC:
    журнал пишется на этой машине, и молча сдвинуть все записи на часы хуже, чем догадаться
    о локальной зоне."""
    m = _TS_RE.match(str(raw or "").strip())
    if not m:
        return None
    frac = (m.group(3) or "0")[:6].ljust(6, "0")
    tz = m.group(4) or ""
    try:
        ts = datetime.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}.{frac}"
                                            f"{'+00:00' if tz == 'Z' else tz}")
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.astimezone()


def applied_in_window(rows: list[Any], *, hours: int = ROLLING_WINDOW_H,
                      moment: datetime.datetime | None = None) -> int:
    """Сколько РАЗНЫХ вакансий журнал знает как откликнутые в окне `hours` до `moment`.

    Дедуп по id — как в `hh_sync._journal_applied_today`: синк дожурналирует ту же вакансию
    вторым каналом (ручной отклик на hh.ru + подхват из чата), и без дедупа окно насчитало бы
    лишние отклики, то есть придушило бы темп на ровном месте. Строки с неразбираемым `ts`
    не считаем: недосчитать одну запись дешевле, чем остановить прогон по мусорной строке."""
    now = moment if moment is not None else datetime.datetime.now(datetime.timezone.utc)
    edge = now - datetime.timedelta(hours=hours)
    seen: set[str] = set()
    for e in rows:
        ts = journal_ts(str(e.get("ts") or ""))
        if ts is not None and edge < ts <= now:
            seen.add(str(e.get("id")))
    return len(seen)


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
