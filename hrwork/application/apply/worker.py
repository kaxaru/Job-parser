"""Тёплый воркер откликов для сервера ленты: один браузер на серию кликов.

Вынесено из `autoclick.py` (аудит 22.09.2026, §5). Потребитель — `presentation/server.py`
(`/api/apply`); отклик выполняет `autoclick._apply_one_vacancy` и очередь дожимает
`autoclick._drain_pending` — поэтому модуль зависит от `autoclick`, обратной стрелки нет.

Проблема наивного /api/apply: каждый клик поднимал НОВЫЙ браузер (старт + DDoS-Guard
+ логин) и закрывал — медленно и расточительно. Воркер держит ОДИН браузер в выделенном
потоке (Playwright sync НЕ потокобезопасен — работаем из одного потока), переиспользует
его между кликами, а по простою IDLE_TIMEOUT закрывает и отпускает lock (чтобы не
блокировать крон). Запросы шлют задания в очередь и ждут результат.
"""
import contextlib
import queue
import threading
from typing import Any

from hrwork.application.apply import account_session, autoclick, browser
from hrwork.application.apply.runtime import lock
from hrwork.application.apply.runtime.quota import DAILY_CAP_DEFAULT
from hrwork.config import log


def _start_playwright() -> Any:
    """Запуск Playwright, вынесен для тестируемости (мокается в тестах воркера)."""
    from playwright.sync_api import sync_playwright
    return sync_playwright().start()


class ApplyWorker:
    IDLE_TIMEOUT = 300   # сек без кликов -> закрыть браузер, отпустить lock (пустить крон)
    RESULT_TIMEOUT = 600  # предохранитель: submit не виснет вечно, даже если поток умер молча

    def __init__(self, headless: bool = True):
        self._q: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._headless = headless

    def submit(self, vid: str, url: str, cover: str, name: str = "",
               employer: str = "") -> dict[str, Any]:
        """Поставить отклик в очередь и дождаться результата (блокирует поток запроса).
        `employer` опционален и нужен только журналу (см. `autoclick._apply_one_vacancy`)."""
        done = threading.Event()
        box: dict[str, Any] = {}
        # Кладём в очередь ДО старта потока: иначе поток мог бы упасть и слить пустую очередь
        # раньше, чем задание попадёт в неё (гонка -> вечное ожидание). finally потока сольёт
        # это задание статусом 'error', SystemExit-ветка — 'busy'.
        self._q.put((str(vid), url, cover, name, employer, done, box))
        self._ensure_thread()
        if not done.wait(timeout=self.RESULT_TIMEOUT):
            log.error("ApplyWorker: результат не пришёл за {}с — таймаут", self.RESULT_TIMEOUT)
            return {"status": "error", "letter": False, "error": "timeout"}
        res: dict[str, Any] = box.get("result", {"status": "error", "letter": False})
        return res

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        try:
            # короткий ретрай: транзиентный lock (быстрая задача) успеет освободиться;
            # длинный крон-батч (~30 мин) не переждать в HTTP-запросе -> честный busy.
            cm = lock._single_instance(wait_retries=2, wait_s=15)
            cm.__enter__()
        except SystemExit:
            self._drain("busy")           # lock занят (крон делает отклики) -> задачам busy
            return
        p = ctx = None
        try:
            p = _start_playwright()
            ctx = browser._launch(p, headless=self._headless)
            page = browser._page(ctx)
            state = browser._session_state(page)
            if state is browser.LoginState.UNKNOWN:
                # страница не доехала: это НЕ «нет сессии», паролем не входим (аудит 22.09.2026, §1).
                # Клики вернут no-session — честнее, чем логиниться при живой сессии.
                log.warning("ApplyWorker: состояние сессии неизвестно (страница не доехала) — "
                            "автовход не пробую, клики вернут no-session")
                logged = False
            elif state is browser.LoginState.ANONYMOUS:
                logged = browser._auto_login(page) and browser._session_state(page) is browser.LoginState.LOGGED_IN
            else:
                logged = True
            # чужая личность в сессии = «нет сессии»: кликать от неё нельзя (RFC-004)
            logged = logged and account_session.verify_session()
            log.info("ApplyWorker: браузер поднят (logged_in={}), жду клики…", logged)
            while True:
                try:
                    vid, url, cover, name, emp, done, box = self._q.get(
                        timeout=self.IDLE_TIMEOUT)
                except queue.Empty:
                    log.info("ApplyWorker: простой {}с — закрываю браузер, отпускаю lock",
                             self.IDLE_TIMEOUT)
                    break
                try:
                    box["result"] = ({"status": "no-session", "letter": False} if not logged
                                     else autoclick._apply_one_vacancy(page, vid, url, cover,
                                                                       name, emp))
                except Exception as e:
                    box["result"] = {"status": "error", "letter": False, "error": str(e)}
                finally:
                    done.set()
                if logged:                        # владеем браузером -> дожать очередь ленты
                    try:
                        autoclick._drain_pending(page, DAILY_CAP_DEFAULT)
                    except Exception as e:
                        # Глушитель без лога прятал ошибку ДО обработчика (напр. store.applied_today()):
                        # в логе не появлялось даже строки «Очередь ленты: обработано N» (аудит §1).
                        log.error("дренаж очереди упал: {}", e)
        except Exception as e:                    # браузер не поднялся / упал в цикле — не роняем поток
            log.error("ApplyWorker: браузер/цикл упал: {}", e)
        finally:
            with contextlib.suppress(Exception):
                if ctx is not None:
                    ctx.close()
            with contextlib.suppress(Exception):
                if p is not None:
                    p.stop()
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)     # отпустить single-instance lock
            with self._start_lock:
                self._thread = None
            # Крэш до/во время цикла (напр. браузер не поднялся) оставил бы задания в очереди
            # с невыставленным done -> HTTP-поток завис бы навсегда. Сливаем остаток ошибкой.
            self._drain("error")

    def _drain(self, status: str) -> None:
        """Слить очередь заданным статусом (напр. busy, когда lock занят кроном)."""
        while True:
            try:
                *_, done, box = self._q.get_nowait()
            except queue.Empty:
                break
            box["result"] = {"status": status, "letter": False}
            done.set()


_apply_worker: "ApplyWorker | None" = None
_apply_worker_lock = threading.Lock()


def get_apply_worker(headless: bool = True) -> ApplyWorker:
    """Синглтон тёплого воркера для сервера (создаётся лениво, потокобезопасно).
    Сервер — ThreadingHTTPServer: без лока два параллельных POST /api/apply создали бы
    два воркера -> два Chromium на один persistent-профиль -> краш."""
    global _apply_worker
    if _apply_worker is None:                    # double-checked: быстрый путь без лока
        with _apply_worker_lock:
            if _apply_worker is None:
                _apply_worker = ApplyWorker(headless=headless)
    return _apply_worker
