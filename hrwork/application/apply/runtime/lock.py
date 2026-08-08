"""Single-instance lock: persistent-профиль Chromium нельзя открыть двумя браузерами.

Lock-файл с pid+ts; живой свежий инстанс держит его, мёртвый/протухший (> LOCK_TTL) —
отдаёт. Вынесено из autoclick — файловая логика без Playwright, тестируется напрямую."""
import contextlib
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from hrwork.config import DATA_DIR, log

LOCK_FILE = DATA_DIR / "autoclick.lock"          # один браузер на persistent-профиль
# TTL должен ПОКРЫВАТЬ самый долгий прогон: батч 25 откликов ~30 мин, полный на 200 —
# до ~4ч. Меньший TTL ложно счёл бы живой батч «протухшим» → второй Chromium на профиль
# → краш. Мёртвый pid отдаёт lock сразу (см. _lock_holder); TTL страхует лишь случаи, где
# живость определить нельзя: reuse pid и процесс, вышедший с кодом ровно 259.
LOCK_TTL  = 4 * 3600                              # 4 часа
_STILL_ACTIVE = 259                               # GetExitCodeProcess: процесс ещё бежит


def _pid_alive(pid: int) -> bool:
    """Бежит ли процесс СЕЙЧАС.

    ИНЦИДЕНТ 25.07.2026: watchdog расстрелял дерево (pid 26652 вышел с кодом 1), но
    `OpenProcess` на него по-прежнему УСПЕВАЛ — объект процесса живёт в таблице, пока
    чужой хендл его держит, хотя в списке процессов его уже нет. Проверка «хендл открылся
    → жив» врала, `_lock_holder` считала lock занятым, и крон-слоты откликов отваливались
    3 часа с «lock занят (pid=26652)» — до истечения LOCK_TTL. Открытие хендла живость НЕ
    доказывает: спрашиваем код выхода."""
    if not pid:
        return False
    with contextlib.suppress(Exception):
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True      # спросить не смогли — считаем живым, профиль вслепую не отдаём
            return code.value == _STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    with contextlib.suppress(Exception):     # не-Windows фолбэк
        os.kill(pid, 0)
        return True
    return False


def _lock_holder() -> tuple[str | int, int] | None:
    """(pid, age_s), если lock держит ЖИВОЙ свежий инстанс; иначе None (свободен/протух).
    pid — int из записи lock, либо строка "?" для битого файла (возраст берётся по mtime):
    он только логируется, поэтому сентинел допустим."""
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


def _try_acquire() -> bool:
    """Создать lock-файл АТОМАРНО (O_CREAT|O_EXCL). False — файл уже существует.

    ИНЦИДЕНТ-КЛАСС (аудит 08.08.2026): до этого захват был check-then-write — `_lock_holder()`
    и следом безусловный `write_text`. Крон-слот и ручной `hh.py forms` могли пройти проверку
    в одно окно и оба «взять» lock -> два Chromium на один persistent-профиль (то, ради чего
    lock и существует) + потерянный инкремент квоты. Окно узаконил watchdog-путь: `_kill_own_tree`
    отдаёт lock ДО выстрела, пока Chromium ещё умирает. O_EXCL отдаёт файл ровно одному
    претенденту — это гарантия ФС, а не порядка вызовов.

    Восстановление: файл либо создан целиком (с pid+ts), либо не создан вовсе. Упавший между
    созданием и записью тела оставит пустой файл — его подберёт ветка «битый lock» в
    `_lock_holder` (свежий -> держим, старше TTL -> отдаём)."""
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    # Прочие OSError (нет каталога, нет прав) НЕ глушим: без lock браузер поднимать нельзя,
    # и тихое «продолжаем» вернуло бы второй Chromium на persistent-профиль.
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    return True


def _drop_stale() -> bool:
    """Снять lock, который `_lock_holder` признал протухшим/осиротевшим. True — сняли.
    Отдельным шагом от захвата: O_EXCL не перезаписывает, поэтому мёртвый файл сперва убираем,
    а потом честно соревнуемся за создание нового.

    Живость перепроверяем ПОВТОРНО, вплотную к unlink: пока мы решались, конкурент мог снять
    тот же протухший файл и положить свой, ЖИВОЙ, — снести его значило бы пустить второй
    Chromium на persistent-профиль. Остаточное окно (микросекунды между проверкой и unlink)
    признано осознанным: закрыть его полностью можно только собственным арбитром, а прежний
    код держал окно РАЗМЕРОМ С ВЕСЬ ЗАХВАТ и не имел даже атомарного создания."""
    if _lock_holder() is not None:
        return False
    try:
        LOCK_FILE.unlink(missing_ok=True)
        return True
    except OSError as e:
        log.warning("протухший lock не удалось снять ({})", e)
        return False


def release_if_mine() -> bool:
    """Снять lock, если его держит ЭТОТ процесс. True — сняли.

    Нужно аварийному выходу: watchdog расстреливает СОБСТВЕННОЕ дерево
    (`autoclick.py::_kill_own_tree`), поэтому `finally` в `_single_instance` не наступает и
    файл остаётся с мёртвым pid. Чужой lock не трогаем никогда — иначе второй Chromium
    на persistent-профиль."""
    with contextlib.suppress(OSError, TypeError, ValueError):
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if int(info.get("pid", 0)) == os.getpid():
            LOCK_FILE.unlink(missing_ok=True)
            return True
    return False


@contextmanager
def _single_instance(wait_retries: int = 0, wait_s: float = 60) -> Iterator[None]:
    """Отказ, если уже бежит свежий инстанс (lock не старше LOCK_TTL и PID жив).
    Протухший/осиротевший lock перезаписываем.

    wait_retries>0 — не падать сразу, а ЖДАТЬ освобождения (для крона: сервер ленты держит
    ТОТ ЖЕ lock ~300с после отклика — иначе крон-цикл пропускался бы впустую).

    Захват АТОМАРЕН (`_try_acquire`, O_EXCL): проверка живости и создание файла больше не
    разнесены во времени, поэтому два претендента не могут «оба взять» свободный lock."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in range(wait_retries + 1):
        if _try_acquire():                             # свободен — взяли, гонки нет
            break
        held = _lock_holder()
        if held is None and _drop_stale() and _try_acquire():
            break                                      # держал мёртвый/протухший — перезабрали
        # held is None и захват не удался -> lock успел взять другой претендент: он и хозяин
        pid, age = held if held is not None else ("?", 0)
        if attempt < wait_retries:
            log.info("lock занят (pid={}, {}с) — жду {}с и повторю ({}/{})",
                     pid, age, int(wait_s), attempt + 1, wait_retries)
            time.sleep(wait_s)
        else:
            raise SystemExit(
                f"autoclick уже выполняется (lock, pid={pid}, {age}с) — выходим")
    try:
        yield
    finally:
        # Снимаем ТОЛЬКО свой файл: прогон, переживший LOCK_TTL, к этому моменту мог уже
        # лишиться lock (другой инстанс счёл его протухшим и перезабрал) — безусловный unlink
        # снёс бы чужой lock и пустил второй Chromium на профиль.
        release_if_mine()
