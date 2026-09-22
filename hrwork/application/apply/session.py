"""Сессия hh.ru БЕЗ браузера: куки из persistent-профиля -> чистый HTTP.

Зачем: чат-эндпоинты (chatik.hh.ru) — cookie-only, их НЕ гейтит Group-IB fingerprint
(в отличие от отклика и /resumes/touch). Раньше `sync_statuses` ради этих JSON-вызовов
поднимал целый Chromium, проходил DDoS-Guard и занимал общий `autoclick.lock` — из-за чего
синк конкурировал с откликами и намертво вставал вместе с ними (статусы в проекте застряли
на две недели).

Теперь браузерный прогон сохраняет состояние сессии (`save_state`), а синк работает
поверх него обычным httpx-клиентом: без Chromium, без lock, параллельно чему угодно.

`CookieRequestContext` намеренно повторяет форму Playwright APIRequestContext
(`.get/.post` -> объект с `.status`/`.json()`), поэтому `chat.py` не знает, кто его зовёт,
и не меняется вовсе.
"""
import contextlib
import os
from typing import Any

from hrwork.config import ACCOUNT_DIR, log
from hrwork.infrastructure.sources.hh import BROWSER_UA
from hrwork.infrastructure.storage import read_json_or

STATE_FILE = ACCOUNT_DIR / "hh_state.json"   # storage_state браузера (куки сессии), свой у аккаунта
XSRF_COOKIE = "_xsrf"
_TIMEOUT_S = 25


def save_state(ctx: Any) -> None:
    """Выгрузить куки живого браузерного контекста в STATE_FILE (зовётся после проверки
    логина в каждом браузерном прогоне — так состояние обновляется само).

    АТОМАРНО (аудит 22.09.2026, §5): `ctx.storage_state` пишет файл САМ и в память состояние
    не отдаёт, поэтому пишем во временный путь рядом и подменяем `os.replace`. Иначе синк в
    другом процессе (он этот файл читает, lock не берёт — это его проектное свойство) мог
    поймать недописанный JSON: `read_json_or` вернул бы пусто -> «Нет сохранённой сессии» и
    весь синк пропущен."""
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(tmp))
        os.replace(tmp, STATE_FILE)
        log.debug("Состояние сессии сохранено: {}", STATE_FILE)
    except Exception as e:                       # не роняем прогон отклика из-за экспорта
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)          # недописанный временный файл не копим
        log.warning("Не удалось сохранить состояние сессии: {}", e)


def hh_cookies(state: Any) -> dict[str, str]:
    """{имя: значение} кук домена hh.ru из состояния сессии браузера ({} — если их нет).

    ЕДИНЫЙ разборщик `state["cookies"]`: до 23.09.2026 фильтр по домену был скопирован в
    `session._cookies_and_xsrf` и `account_session.identity_from_state` (аудит 22.09.2026,
    §3.2), и правка одного (напр. суффикс `hh.ru` -> список доменов) молча расходилась со
    вторым — а расхождение тут стоит либо «сессии нет» на живых куках, либо сверки личности
    не с тем пользователем HH (необратимые отклики не от того резюме, RFC-004).

    Контракт МЯГКИЙ: `state` — внешний JSON, поэтому что угодно, кроме словаря, даёт {}."""
    if not isinstance(state, dict):
        return {}
    return {str(c["name"]): str(c["value"]) for c in (state.get("cookies") or [])
            if isinstance(c, dict) and c.get("name")
            and str(c.get("domain", "")).endswith("hh.ru")}


def _cookies_and_xsrf() -> tuple[dict[str, str], str]:
    """{имя: значение} кук hh.ru + xsrf-токен. ({}, '') — состояния нет/битое."""
    jar = hh_cookies(read_json_or(STATE_FILE, {}))
    return jar, jar.get(XSRF_COOKIE, "")


class _Resp:
    """Ответ в форме Playwright APIResponse (.status/.json()) поверх httpx.Response."""

    __slots__ = ("_r", "status")

    def __init__(self, r: Any):
        self._r = r
        self.status = r.status_code

    def json(self) -> Any:
        return self._r.json()


class CookieRequestContext:
    """Дак-тайп замена Playwright APIRequestContext для cookie-only эндпоинтов."""

    def __init__(self, client: Any):
        self._c = client

    def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return _Resp(self._c.get(url, headers=headers))

    def post(self, url: str, headers: dict[str, str] | None = None,
             data: Any = None) -> Any:
        return _Resp(self._c.post(url, headers=headers, content=data))

    def __enter__(self) -> "CookieRequestContext":
        return self

    def __exit__(self, *_: Any) -> None:
        self._c.close()


def open_client() -> tuple[CookieRequestContext | None, str]:
    """(контекст запросов, xsrf) поверх сохранённых кук. (None, '') — сессии нет.
    Протухшие куки здесь НЕ детектим: вызовы chat.* сами вернут пусто (они suppress'ят
    ошибки), а обновит их следующий браузерный прогон."""
    if not STATE_FILE.exists():
        return None, ""
    jar, xsrf = _cookies_and_xsrf()
    if not jar:
        return None, ""
    import httpx
    client = httpx.Client(cookies=jar, timeout=_TIMEOUT_S, follow_redirects=True,
                          headers={"user-agent": BROWSER_UA})
    return CookieRequestContext(client), xsrf


def state_age_hint() -> str:
    """Человекочитаемая метка свежести состояния — для логов синка."""
    if not STATE_FILE.exists():
        return "нет файла"
    import datetime
    ts = datetime.datetime.fromtimestamp(STATE_FILE.stat().st_mtime)
    return ts.strftime("%Y-%m-%d %H:%M")


__all__ = ["STATE_FILE", "CookieRequestContext", "hh_cookies", "open_client", "save_state",
           "state_age_hint"]

