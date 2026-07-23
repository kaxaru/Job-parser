"""Дневная квота откликов (HH режет на ~200/сутки), идемпотентно между запусками дня.

Счётчик в apply_quota.json ({"date","count"}); запись за прошлый день = 0. Вынесено из
autoclick — чистая логика над файлом, без браузера, тестируется напрямую."""
import datetime

from hrwork.config import DATA_DIR, HH_DAILY_APPLY_CAP
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

QUOTA_FILE = DATA_DIR / "apply_quota.json"       # {"date": "YYYY-MM-DD", "count": N}
DAILY_CAP_DEFAULT = HH_DAILY_APPLY_CAP           # потолок откликов в сутки (лимит HH, config)


def _today() -> str:
    return datetime.date.today().isoformat()


def _load_quota() -> dict:
    return read_json_or(QUOTA_FILE, {})


def applied_today(quota: dict | None = None) -> int:
    """Сколько откликов уже сделано СЕГОДНЯ (0, если запись за прошлый день)."""
    q = quota if quota is not None else _load_quota()
    return int(q.get("count", 0)) if q.get("date") == _today() else 0


def bump_quota(n: int) -> int:
    """Прибавить n к сегодняшнему счётчику (атомарно). Возвращает новый итог."""
    if n <= 0:
        return applied_today()
    total = applied_today() + n
    atomic_write_json(QUOTA_FILE, {"date": _today(), "count": total})
    return total
