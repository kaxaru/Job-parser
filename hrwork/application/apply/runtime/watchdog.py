"""Watchdog зависшего прогона: снос собственного дерева процессов по дедлайну.

Вынесено из `autoclick.py` (аудит 22.09.2026, §5): модуль не касается Playwright, зависит
только от `log`/`faulthandler`/`threading` и отдачи lock.

ПОДТВЕРЖДЕНО стеком (19.07): виснет `page.wait_for_selector(_HH_ROOT, timeout=20_000)`
внутри навигации — вызов с 20-секундным таймаутом простоял 50 минут. Причина: таймауты
Playwright исполняет NODE-драйвер, и когда связка драйвер/CDP клинит, python блокируется в
`_sync()` на трубе НАВСЕГДА — собственный таймаут Playwright не спасает. Нужен внешний дедлайн.

ExecutionTimeLimit планировщика для этого НЕ годится: он снимает только .bat, а python +
node + chromium ОСИРОТЕВАЮТ и продолжают держать профиль и autoclick.lock (наблюдали 19.07 —
после «завершения» задачи жило 10 процессов). Поэтому убиваем дерево сами: taskkill /T по
собственному pid снимает и node-драйвер, и Chromium.

Пороги ПРОВЕРЕНЫ и оставлены прежними (замер 03.08.2026 по 128 прогонам): здоровый прогон
идёт медиана 27 мин, p90 36, максимум 37.4 — снижать 50 минут некуда, любое ужесточение
начинает резать рабочие прогоны, а выигрыш (45 вместо 50) в пределах шума.
Дедлайна на ОТДЕЛЬНЫЙ Playwright-вызов не существует: sync-API привязан к своему потоку
(вызов из чужого валит драйвер с greenlet.error), а залипший вызов отпускает python только
со смертью процесса. Поэтому watchdog остаётся ЕДИНСТВЕННЫМ средством против зависания,
а от МОЛЧАЛИВОЙ деградации защищает предохранитель `APPLY_SKIP_STREAK_MAX`.
"""
import contextlib
import faulthandler
import os
import subprocess
import sys
import threading
from collections.abc import Iterator

from hrwork.application.apply.runtime.lock import release_if_mine
from hrwork.config import log

WATCHDOG_DUMP_S = 40 * 60   # стек всех потоков в лог — диагностика
WATCHDOG_KILL_S = 50 * 60   # жёсткий снос дерева (до ExecutionTimeLimit=60м, тот уже не нужен)


def _kill_own_tree() -> None:
    """Снести себя вместе с потомками (node-драйвер + Chromium). Вызывается из таймера-демона,
    когда основной поток намертво заблокирован в Playwright и вернуть управление невозможно."""
    log.error("WATCHDOG: прогон завис (>{} мин) — снимаю дерево процессов", WATCHDOG_KILL_S // 60)
    faulthandler.dump_traceback(file=sys.stderr)          # стек ПЕРЕД сносом
    # ИНЦИДЕНТ 25.07.2026: taskkill /T снимает и НАС САМИХ, поэтому `finally` в
    # _single_instance не наступает — lock оставался с мёртвым pid. Отдаём его сами, ДО
    # выстрела, иначе файл переживает прогон и блокирует крон-слоты. Осознанная цена —
    # под-секундное окно, в котором ждущий инстанс может взять lock, пока наш Chromium ещё
    # умирает (docs/errors.md: «Осиротевший lock»); выигрыш — иммунитет к reuse pid.
    if release_if_mine():
        log.info("WATCHDOG: autoclick.lock отдан до сноса дерева")
    with contextlib.suppress(Exception):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                       capture_output=True, timeout=30)
    with contextlib.suppress(Exception):                  # если taskkill не отработал
        os._exit(1)


@contextlib.contextmanager
def _hang_watchdog(dump_s: int = WATCHDOG_DUMP_S,
                   kill_s: int = WATCHDOG_KILL_S) -> Iterator[None]:
    """Дедлайн прогона: дамп стека на `dump_s`, снос дерева на `kill_s`."""
    faulthandler.dump_traceback_later(dump_s, exit=False)
    killer = threading.Timer(kill_s, _kill_own_tree)
    killer.daemon = True
    killer.start()
    try:
        yield
    finally:
        killer.cancel()
        faulthandler.cancel_dump_traceback_later()
