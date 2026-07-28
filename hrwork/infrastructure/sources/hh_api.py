"""Официальный API hh.ru — ВТОРОЙ путь рядом с браузерным (`sources/hh.py` + `apply/autoclick`).

Почему появился. В шапке `sources/hh.py` записано «API закрыт DDoS-Guard» — 28.07 это
опровергнуто проверкой: запрос доходит до самого HH (`Server-Timing: frontik`), и без токена
приходит `{"errors":[{"value":"bad_authorization","type":"oauth"}]}`. API не закрыт, ему нужен
OAuth-токен. Отсюда весь этот модуль.

Что даёт: сбор структурированным JSON вместо разбора HTML и отклик HTTP-запросом вместо
браузера — а значит без капчи `/account/captcha`, без ожидания DDoS-Guard, без `autoclick.lock`
и зависаний Playwright. Чего НЕ даёт: анкеты-опросники живут только в вебе, там браузер остаётся.

Ключи — СВОЕГО приложения с dev.hh.ru (`HH_CLIENT_ID`/`HH_CLIENT_SECRET`). Чужие client_id
официального приложения HH здесь не используются и использоваться не будут: это выдача себя
за чужое приложение, за которую отвечает аккаунт пользователя.
"""
from __future__ import annotations

import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import ClassVar

import httpx

from hrwork.config import (
    HH_ACCESS_TOKEN,
    HH_API_UA,
    HH_CLIENT_ID,
    HH_CLIENT_SECRET,
    HH_REDIRECT_URI,
    HH_TOKEN_FILE,
    log,
)
from hrwork.infrastructure.storage.jsonio import atomic_write_json

API_BASE = "https://api.hh.ru"
AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = f"{API_BASE}/token"          # оба живы (проверено), документирован этот
_EXPIRY_MARGIN = 300                     # обновляем за 5 мин до конца, чтобы не ловить 403 в бою
_TIMEOUT = 20.0


class HhApiError(Exception):
    """Ошибка API с сохранённым телом: по нему видно, отказал HH или заслон перед ним."""

    def __init__(self, status: int, payload: dict | str):
        super().__init__(f"HTTP {status}: {str(payload)[:300]}")
        self.status = status
        self.payload = payload


@dataclass(frozen=True)
class TokenSet:
    """Пара токенов + момент истечения. `expires_in` от HH — относительный, храним АБСОЛЮТНЫЙ
    момент: иначе после перезапуска процесса неясно, сколько токену осталось."""
    access: str
    refresh: str
    expires_at: float

    @classmethod
    def from_response(cls, data: dict) -> TokenSet:
        return cls(access=str(data.get("access_token") or ""),
                   refresh=str(data.get("refresh_token") or ""),
                   expires_at=time.time() + float(data.get("expires_in") or 0))

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - _EXPIRY_MARGIN

    def to_dict(self) -> dict:
        return {"access_token": self.access, "refresh_token": self.refresh,
                "expires_at": self.expires_at}


def load_token() -> TokenSet | None:
    """Токен с диска (или из `HH_ACCESS_TOKEN` — ручной override без refresh)."""
    if HH_ACCESS_TOKEN:
        return TokenSet(access=HH_ACCESS_TOKEN, refresh="", expires_at=time.time() + 3600)
    try:
        d = json.loads(HH_TOKEN_FILE.read_text(encoding="utf-8"))
        return TokenSet(access=str(d["access_token"]), refresh=str(d.get("refresh_token") or ""),
                        expires_at=float(d.get("expires_at") or 0))
    except (OSError, ValueError, KeyError):
        return None


def save_token(t: TokenSet) -> None:
    atomic_write_json(HH_TOKEN_FILE, t.to_dict())


def _headers(access: str = "") -> dict:
    # HH-User-Agent обязателен по правилам API: по нему HH связывается при проблемах.
    h = {"HH-User-Agent": HH_API_UA, "User-Agent": HH_API_UA}
    if access:
        h["Authorization"] = f"Bearer {access}"
    return h


def authorize_url(state: str) -> str:
    """Ссылка, по которой пользователь подтверждает доступ. `state` — защита от подмены
    ответа: сверяем его в callback."""
    if not HH_CLIENT_ID:
        raise ValueError("HH_CLIENT_ID не задан — зарегистрируй приложение на dev.hh.ru")
    q = urllib.parse.urlencode({"response_type": "code", "client_id": HH_CLIENT_ID,
                                "redirect_uri": HH_REDIRECT_URI, "state": state})
    return f"{AUTHORIZE_URL}?{q}"


def _token_request(payload: dict) -> TokenSet:
    r = httpx.post(TOKEN_URL, data=payload, headers=_headers(), timeout=_TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        raise HhApiError(r.status_code, r.text) from None
    if r.status_code != 200 or not data.get("access_token"):
        raise HhApiError(r.status_code, data)
    return TokenSet.from_response(data)


def exchange_code(code: str) -> TokenSet:
    """`code` из редиректа -> токены."""
    return _token_request({"grant_type": "authorization_code", "client_id": HH_CLIENT_ID,
                           "client_secret": HH_CLIENT_SECRET, "redirect_uri": HH_REDIRECT_URI,
                           "code": code})


def refresh(token: TokenSet) -> TokenSet:
    """Обновление по refresh_token — без участия человека. HH выдаёт НОВУЮ пару, старый
    refresh после этого недействителен, поэтому сохраняем сразу."""
    if not token.refresh:
        raise HhApiError(400, "нет refresh_token (ручной override HH_ACCESS_TOKEN?)")
    fresh = _token_request({"grant_type": "refresh_token", "refresh_token": token.refresh})
    save_token(fresh)
    return fresh


class ApiClient:
    """Тонкий клиент: подставляет токен и UA, обновляет протухший токен ОДИН раз.

    Повторяем ровно однажды: если и после обновления `bad_authorization`, дело не в сроке —
    значит нет прав, и молотить запросами бессмысленно (тот же принцип, что у капча-гарда)."""

    def __init__(self, token: TokenSet | None = None):
        self.token = token or load_token()

    def _ensure(self) -> TokenSet:
        if not self.token:
            raise HhApiError(401, "нет токена — сначала: hh.py hhapi --login")
        if self.token.is_expired and self.token.refresh:
            self.token = refresh(self.token)
        return self.token

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None, _retried: bool = False) -> dict:
        t = self._ensure()
        r = httpx.request(method, f"{API_BASE}{path}", params=params, json=json_body,
                          headers=_headers(t.access), timeout=_TIMEOUT)
        if r.status_code in (401, 403) and not _retried and self.token and self.token.refresh:
            body = r.text.lower()
            if "bad_authorization" in body or "token" in body:
                self.token = refresh(self.token)
                return self.request(method, path, params=params, json_body=json_body,
                                    _retried=True)
        if r.status_code >= 400:
            try:
                raise HhApiError(r.status_code, r.json())
            except ValueError:
                raise HhApiError(r.status_code, r.text) from None
        try:
            return r.json()
        except ValueError:
            return {}

    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, **body) -> dict:
        return self.request("POST", path, json_body=body or None)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Ловит редирект `redirect_uri` и кладёт `code` в общий словарь.

    Классовый атрибут, а не поле экземпляра: экземпляр создаёт сам HTTPServer на каждый
    запрос, и достучаться до него снаружи нельзя — результат забирает вызывающий."""
    result: ClassVar[dict] = {}

    def do_GET(self) -> None:                                  # имя задано BaseHTTPRequestHandler
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in q.items()}
        ok = "code" in _CallbackHandler.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Авторизация принята, можно закрыть вкладку." if ok else "Код не получен."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *_args) -> None:                     # тишина: свой лог ведём сами
        return


def login(timeout: float = 180.0) -> TokenSet:
    """Интерактивная авторизация СВОЕГО приложения: браузер -> подтверждение -> localhost.

    Ловим редирект локальным сервером на порту из `HH_REDIRECT_URI` — поэтому не нужны ни
    эмуляция устройства, ни перехват кастомной схемы вроде `hhandroid://`, ни чужие ключи.
    """
    if not (HH_CLIENT_ID and HH_CLIENT_SECRET):
        raise ValueError("HH_CLIENT_ID/HH_CLIENT_SECRET не заданы — зарегистрируй приложение "
                         "на dev.hh.ru и пропиши их в .env")
    parts = urllib.parse.urlsplit(HH_REDIRECT_URI)
    port = parts.port or 80
    state = secrets.token_urlsafe(16)
    _CallbackHandler.result = {}
    srv = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = authorize_url(state)
    log.info("Открываю страницу авторизации HH. Если браузер не открылся — перейди сам:\n{}", url)
    webbrowser.open(url)
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and "code" not in _CallbackHandler.result:
            time.sleep(0.4)
    finally:
        srv.shutdown()
    got = _CallbackHandler.result
    if "code" not in got:
        raise HhApiError(408, f"код не получен за {int(timeout)}с (ответ: {got or 'пусто'})")
    if got.get("state") != state:
        raise HhApiError(400, "state не совпал — ответ не от нашего запроса, прерываю")
    token = exchange_code(got["code"])
    save_token(token)
    log.success("Токен получен и сохранён: {}", HH_TOKEN_FILE)
    return token


# ── Проба: что именно разрешено нашему приложению ────────────────────────────────────
_PROBES = (
    ("GET", "/me", {}, "профиль соискателя"),
    ("GET", "/resumes/mine", {}, "список резюме (нужен для отклика)"),
    ("GET", "/vacancies", {"per_page": 1, "text": "python"}, "поиск вакансий (замена HTML-сбора)"),
    ("GET", "/negotiations", {"per_page": 1}, "отклики/переписка"),
)


def probe() -> dict:
    """Проверка прав БЕЗ побочных эффектов: ни одного отклика не отправляется.

    Отвечает на единственный настоящий вопрос переезда — что доступно нашему приложению.
    Право на отклик (`POST /negotiations`) здесь НЕ дёргается: это необратимое действие,
    его проверяем отдельной осознанной командой на одной вакансии."""
    client = ApiClient()
    out: dict[str, dict] = {}
    for method, path, params, what in _PROBES:
        try:
            data = client.request(method, path, params=params or None)
            keys = list(data)[:6] if isinstance(data, dict) else []
            out[path] = {"ok": True, "what": what, "keys": keys}
            log.success("{} {} -> OK ({})", method, path, what)
        except HhApiError as e:
            out[path] = {"ok": False, "what": what, "status": e.status,
                         "error": str(e.payload)[:200]}
            log.error("{} {} -> HTTP {} ({}): {}", method, path, e.status, what,
                      str(e.payload)[:160])
    return out
