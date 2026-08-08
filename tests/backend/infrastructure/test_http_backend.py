"""Контракт сетевого примитива `net/http.py`: что считается успехом и что уходит в argv.

Оба бэкенда обязаны отвечать ОДИНАКОВО (не-200 / пустое тело / сбой -> None, редиректы
следуются): адаптеры девяти порталов строят на этом свои ретраи, и разъезд означает, что
на боевом (curl) пути троттлинг неотличим от «данные кончились».

Сеть здесь не трогается: curl подменён на фейк, httpx-клиент — на заглушку.
"""
import asyncio
from typing import Any

import pytest

from hrwork.config import log
from hrwork.infrastructure.net import http as H

URL = "https://portal.example/api/jobs?page=1"
BODY = b'{"items": [1, 2, 3]}'
# Пароль прокси — то, что НЕ должно попасть в список аргументов процесса.
PROXY = "socks5://hh_user:s3cr3t-pass@10.0.0.7:1080"


class _FakeCurl:
    """Подмена `asyncio.create_subprocess_exec`: запоминает argv/stdin, отдаёт готовый ответ.

    `status` — то, что curl печатает по `-w` в stderr; `stdout` — тело ответа.
    """

    def __init__(self, stdout: bytes = BODY, status: bytes = b"200", returncode: int = 0) -> None:
        self._stdout, self._status = stdout, status
        self.returncode = returncode
        self.calls = 0
        self.args: list[str] = []
        self.stdin_payload: bytes | None = None

    def __call__(self, *args: str, **kwargs: Any) -> Any:
        self.calls += 1
        self.args = list(args)
        return self._spawn()

    async def _spawn(self) -> "_FakeCurl":
        return self

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_payload = data
        return self._stdout, self._status


def _fetch(monkeypatch: pytest.MonkeyPatch, curl: _FakeCurl,
           url: str = URL, proxy: str | None = None) -> bytes | None:
    monkeypatch.setattr(H, "_BACKEND", "curl")      # боевой дефолт, не зависим от .env прогона
    monkeypatch.setattr(asyncio, "create_subprocess_exec", curl)
    return asyncio.run(H.fetch_bytes(url, headers={"User-Agent": "UA"}, proxy=proxy))


# ── Код ответа: паритет с httpx (аудит 08.08.2026, п.10) ────────────────────────────────
# Было: curl звался без --fail и без разбора кода, поэтому тело 429/503 возвращалось КАК
# УСПЕХ. Адаптер видел «страница пуста» и не ретраил ни разу, тогда как под HTTP_BACKEND=httpx
# тот же ответ давал 4 попытки с бэкоффом.

def test_ok_response_returns_body_bytes(monkeypatch):
    assert _fetch(monkeypatch, _FakeCurl(stdout=BODY, status=b"200")) == BODY


@pytest.mark.parametrize("status", [b"301", b"400", b"404", b"429", b"500", b"503"])
def test_non_200_response_is_not_returned_as_data(monkeypatch, status):
    # тело ошибки портала не должно доехать до адаптера ни под каким кодом
    assert _fetch(monkeypatch, _FakeCurl(stdout=b'{"error": "too many requests"}',
                                         status=status)) is None


def test_empty_body_with_200_returns_none(monkeypatch):
    assert _fetch(monkeypatch, _FakeCurl(stdout=b"", status=b"200")) is None


def test_curl_process_failure_returns_none(monkeypatch):
    # обрыв связи: curl вышел с 7 (couldn't connect) и кода ответа нет
    assert _fetch(monkeypatch, _FakeCurl(stdout=b"", status=b"000", returncode=7)) is None


def test_unreadable_status_returns_none_and_warns_once(monkeypatch):
    """Кода нет -> None по всем запросам, и сбор опустеет. Это обязано быть видно в логе,
    но ровно одной строкой: на прогоне сбора запросов ~20 тысяч."""
    monkeypatch.setattr(H, "_status_warned", False)
    curl = _FakeCurl(stdout=BODY, status=b"curl: unknown --write-out variable")
    seen: list[str] = []
    sink = log.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        assert _fetch(monkeypatch, curl) is None
        assert _fetch(monkeypatch, curl) is None
    finally:
        log.remove(sink)
    assert len(seen) == 1
    assert "HTTP-код" in seen[0]


def test_redirects_are_followed_like_httpx(monkeypatch):
    """follow_redirects=True у httpx -> -L у curl: иначе один и тот же адрес отдаёт
    данные под одним бэкендом и None под другим."""
    curl = _FakeCurl()
    _fetch(monkeypatch, curl)
    assert "-L" in curl.args


@pytest.mark.parametrize("status,expected", [(200, BODY), (429, None), (503, None), (302, None)])
def test_httpx_backend_agrees_with_curl_on_status(monkeypatch, status, expected):
    """Вторая сторона паритета: контракт не должен уехать и со стороны httpx."""
    class _Resp:
        status_code = status
        content = BODY

    class _Client:
        async def get(self, url, headers=None, timeout=None):
            return _Resp()

    monkeypatch.setattr(H, "_get_client", _Client)
    assert asyncio.run(H._httpx_fetch(URL, None, 5)) == expected


# ── argv: разделитель и пароль прокси (аудит 08.08.2026, пп. 48 и 47) ───────────────────

def test_url_goes_after_option_separator(monkeypatch):
    """url приезжает из выдачи портала (getmatch.py::_normalize), а не только из констант:
    без `--` значение вида `-o C:\\...\\file` стало бы опцией curl, а не адресом."""
    evil = "-oC:\\Users\\khide\\pwned.txt"
    curl = _FakeCurl()
    _fetch(monkeypatch, curl, url=evil)
    assert curl.args[-2:] == ["--", evil]


def test_proxy_password_never_reaches_process_arguments(monkeypatch):
    """argv читает любой процесс пользователя (`Get-CimInstance Win32_Process`), поэтому
    опция с паролем уходит в stdin curl (`--config -`), а не в командную строку."""
    curl = _FakeCurl()
    assert _fetch(monkeypatch, curl, proxy=PROXY) == BODY     # запрос через прокси всё ещё работает
    assert [a for a in curl.args if "s3cr3t-pass" in a] == []
    assert "--proxy" not in curl.args
    assert curl.args.count("--config") == 1
    assert curl.stdin_payload == b'proxy = "socks5://hh_user:s3cr3t-pass@10.0.0.7:1080"\n'


def test_direct_request_sends_nothing_to_stdin(monkeypatch):
    curl = _FakeCurl()
    _fetch(monkeypatch, curl)
    assert "--config" not in curl.args
    assert curl.stdin_payload is None


@pytest.mark.parametrize("raw,expected", [
    ('socks5://u:pa"ss@h:1', b'proxy = "socks5://u:pa\\"ss@h:1"\n'),
    ("socks5://u:pa\\ss@h:1", b'proxy = "socks5://u:pa\\\\ss@h:1"\n'),
])
def test_proxy_config_escapes_curl_syntax(raw, expected):
    # внутри кавычек curl-конфига значимы обратный слеш и кавычка — иначе прокси
    # молча не применится, и запросы уйдут с домашнего IP
    assert H._proxy_config(raw) == expected
