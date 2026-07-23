"""Single-instance lock: persistent-профиль Chromium нельзя открыть двумя браузерами.

Lock-файл с pid+ts; живой свежий инстанс держит его, мёртвый/протухший (> LOCK_TTL) —
отдаёт. Вынесено из autoclick — файловая логика без Playwright, тестируется напрямую."""
import contextlib
import json
import os
import time
from contextlib import contextmanager

from hrwork.config import DATA_DIR, log

LOCK_FILE = DATA_DIR / "autoclick.lock"          # один браузер на persistent-профиль
# TTL должен ПОКРЫВАТЬ самый долгий прогон: батч 25 откликов ~30 мин, полный на 200 —
# до ~4ч. Меньший TTL ложно счёл бы живой батч «протухшим» → второй Chromium на профиль
# → краш. Мёртвый pid отдаёт lock сразу (см. _lock_holder); TTL страхует лишь reuse pid.
LOCK_TTL  = 4 * 3600                              # 4 часа


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    with contextlib.suppress(Exception):
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    with contextlib.suppress(Exception):     # не-Windows фолбэк
        os.kill(pid, 0)
        return True
    return False


def _lock_holder():
    """(pid, age_s), если lock держит ЖИВОЙ свежий инстанс; иначе None (свободен/протух)."""
    if not LOCK_FILE.exists():
        return None
    try:
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Битый/недописанный lock НЕ считаем «свободным» вслепую (иначе второй браузер на
        # persistent-профиль). Возраст берём по mtime: свежий -> держим (вероятно живой писатель),
        # старый (> LOCK_TTL) -> отдаём как осиротевший.
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            return None
        return ("?", int(age)) if age < LOCK_TTL else None
    age = time.time() - float(info.get("ts", 0))
    if age < LOCK_TTL and _pid_alive(int(info.get("pid", 0))):
        return info.get("pid"), int(age)
    return None


@contextmanager
def _single_instance(wait_retries: int = 0, wait_s: float = 60):
    """Отказ, если уже бежит свежий инстанс (lock не старше LOCK_TTL и PID жив).
    Протухший/осиротевший lock перезаписываем.

    wait_retries>0 — не падать сразу, а ЖДАТЬ освобождения (для крона: сервер ленты держит
    ТОТ ЖЕ lock ~300с после отклика — иначе крон-цикл пропускался бы впустую)."""
    for attempt in range(wait_retries + 1):
        held = _lock_holder()
        if held is None:
            break                                      # свободен/протух — берём
        if attempt < wait_retries:
            log.info("lock занят (pid={}, {}с) — жду {}с и повторю ({}/{})",
                     held[0], held[1], int(wait_s), attempt + 1, wait_retries)
            time.sleep(wait_s)
        else:
            raise SystemExit(
                f"autoclick уже выполняется (lock, pid={held[0]}, {held[1]}с) — выходим")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            LOCK_FILE.unlink()
