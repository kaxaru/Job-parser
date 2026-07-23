"""Кулдаун поднятия резюме: когда последний раз реально подняли.

HH разрешает бесплатное поднятие раз в 4 часа. Отклики же идут каждые 90 минут, и после
слияния задач (bump+apply в одном браузерном прогоне) без гейта мы бы на КАЖДОМ слоте грузили
тяжёлую /applicant/resumes впустую — лишние ~30с и лишняя поверхность зависания там, где её
можно не иметь. Здесь — маленькое состояние на диске, чтобы дёргать поднятие только по делу.

Чистая логика над файлом (без браузера) — тестируется напрямую, как quota.py.
"""
import datetime

from hrwork.config import DATA_DIR
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

BUMP_FILE = DATA_DIR / "bump_state.json"     # {"last_ok": "ISO-8601"}
BUMP_COOLDOWN_H = 4                          # лимит HH: бесплатное поднятие раз в 4 часа


def last_bump_at() -> datetime.datetime | None:
    """Момент последнего УСПЕШНОГО поднятия (None — ещё не поднимали/битый файл)."""
    raw = (read_json_or(BUMP_FILE, {}) or {}).get("last_ok")
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def hours_since_bump() -> float | None:
    """Часов с последнего успешного поднятия (None — не поднимали)."""
    at = last_bump_at()
    if at is None:
        return None
    return (datetime.datetime.now(tz=at.tzinfo) - at).total_seconds() / 3600


def bump_due(cooldown_h: int = BUMP_COOLDOWN_H) -> bool:
    """Пора ли поднимать: кулдаун вышел или ещё ни разу не поднимали."""
    since = hours_since_bump()
    return since is None or since >= cooldown_h


def mark_bumped() -> None:
    """Отметить успешное поднятие (зовётся ТОЛЬКО когда кнопка реально нажалась)."""
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_json(BUMP_FILE, {"last_ok": now})
