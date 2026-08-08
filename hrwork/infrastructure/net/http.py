"""Сетевой примитив сбора: один GET -> bytes|None. Бэкенд выбирается config.HTTP_BACKEND.

- 'curl' (дефолт): отдельный процесс curl на запрос. Стабилен на Windows+VPN, где aiohttp падал
  с WinError 64; минус — спавн процесса + новый TLS на каждый запрос (потолок скорости сбора).
- 'httpx': ОДИН пулированный AsyncClient (keep-alive переиспользует TCP/TLS) — кратно быстрее на
  беспроксишном пути (hirify, HH без прокси). Проксированные запросы (ротация IP у HH) пул не
  умеет -> для них откат на curl автоматически. Экспериментальный флаг, A/B на своей сети.

Ретраи/бэкофф и валидацию контента делают вызывающие (у HH и hirify они осознанно разные).
Контракт у обоих бэкендов ОДИН: не-200, пустое тело или сбой -> None, редиректы следуются.
Разъезд здесь стоит дорого: под curl (боевой дефолт) 429/503 с телом возвращались как успех,
и адаптер принимал троттлинг за «данные кончились» — ни одного ретрая (аудит 08.08.2026).
"""
import asyncio
import re
import shutil
from typing import Any

from hrwork.config import CURL_MAX_TIME, HTTP_BACKEND, log

CURL = shutil.which("curl") or "curl"
# Код ответа curl печатает в stderr (`%{stderr}`), а НЕ хвостом к телу: тело остаётся
# байт-в-байт тем, что отдал сервер. Вариант `-w '%{http_code}'` (код в stdout) требовал бы
# срезать три байта с каждого ответа, и цена промаха там — порча HTML/JSON у всех девяти
# источников, а не пропущенный ретрай. Вариант `--fail` проще, но кода не даёт вовсе:
# не-200 из диапазона 2xx/3xx он пропускает, то есть паритета с httpx не даёт.
_STATUS_FMT = "%{stderr}%{http_code}"
_STATUS_RE = re.compile(rb"(\d{3})\s*\Z")
_status_warned = False
# Размер httpx-пула: с запасом над суммарной конкурентностью источников
# (CONCURRENCY×BATCH_MULT у HH, PAGE/ENRICH_CONCURRENCY у hirify) — пул не должен стать
# бутылочным горлышком поверх семафоров вызывающих.
HTTPX_POOL_SIZE = 40

# Эффективный бэкенд: httpx только если реально установлен, иначе тихий откат на curl.
_BACKEND = HTTP_BACKEND
if _BACKEND == "httpx":
    try:
        import httpx  # noqa: F401
        log.info("HTTP-бэкенд: httpx (пул keep-alive; проксированные запросы -> curl)")
    except ImportError:
        log.warning("HTTP_BACKEND=httpx, но httpx не установлен (pip install httpx) — откат на curl")
        _BACKEND = "curl"

_client = None          # ленивый пулированный httpx.AsyncClient
_client_loop = None     # event loop, к которому привязан клиент


def _get_client() -> Any:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    # Клиент привязан к loop; при новом asyncio.run() (другой loop) пересоздаём, иначе
    # получили бы «Event loop is closed». Старый клиент утечёт до выхода процесса — приемлемо.
    if _client is None or _client_loop is not loop:
        import httpx
        _client = httpx.AsyncClient(
            timeout=CURL_MAX_TIME, follow_redirects=True,
            limits=httpx.Limits(max_connections=HTTPX_POOL_SIZE,
                                max_keepalive_connections=HTTPX_POOL_SIZE))
        _client_loop = loop
    return _client


async def fetch_bytes(url: str, *, headers: dict[str, Any] | None = None, proxy: str | None = None,
                      max_time: int = CURL_MAX_TIME) -> bytes | None:
    """GET url -> сырые bytes (или None при сбое / не-200 / пустом ответе). headers/proxy —
    структурно (бэкенд сам переведёт). Проксированный запрос под httpx-флагом идёт через curl."""
    if _BACKEND == "httpx" and proxy is None:
        return await _httpx_fetch(url, headers, max_time)
    return await _curl_fetch(url, headers, proxy, max_time)


def _http_status(err: bytes) -> int | None:
    """Код ответа из write-out curl (хвост stderr). None — curl кода не дал."""
    m = _STATUS_RE.search(err)
    return int(m.group(1)) if m else None


def _warn_no_status(err: bytes) -> None:
    """Кода нет -> None по ВСЕМ запросам, и сбор молча опустеет. Молчать тут нельзя, но и
    строка на каждый из ~20k запросов прогона — не лог, поэтому предупреждаем один раз."""
    global _status_warned
    if _status_warned:
        return
    _status_warned = True
    log.warning("curl не вернул HTTP-код (-w '{}'), stderr={!r} — сбор вернёт пусто",
                _STATUS_FMT, err[:120])


def _proxy_config(proxy: str) -> bytes:
    """Строка curl-конфига с прокси: уходит в stdin (`--config -`), а не в argv.

    В `socks5://user:pass@host:port` лежит пароль, а argv любого процесса читает любой
    процесс пользователя (`Get-CimInstance Win32_Process`) — `mask_proxy()` закрывает
    только лог. Альтернатива `ALL_PROXY` отвергнута: окружение процесса тоже читается
    и вдобавок НАСЛЕДУЕТСЯ потомками, а stdin живёт ровно один запрос.
    Экранирование — по правилам curl-конфига: внутри кавычек значимы `\\` и `"`.
    """
    escaped = proxy.replace("\\", "\\\\").replace('"', '\\"')
    return f'proxy = "{escaped}"\n'.encode()


async def _curl_fetch(url: str, headers: dict[str, Any] | None, proxy: str | None,
                      max_time: int) -> bytes | None:
    args = [CURL, "-s", "--compressed", "-L", "--max-time", str(max_time), "-w", _STATUS_FMT]
    for k, v in (headers or {}).items():
        args += (["-A", v] if k.lower() == "user-agent" else ["-H", f"{k}: {v}"])
    cfg = _proxy_config(proxy) if proxy else None
    if cfg is not None:
        args += ["--config", "-"]
    # `--` обязателен: url приходит и из выдачи портала (getmatch.py::_normalize), а
    # значение вида `-o C:\...\file` без разделителя стало бы опцией curl, а не адресом.
    args += ["--", url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if cfg is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(cfg)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    status = _http_status(err)
    if status is None:
        _warn_no_status(err)
        return None
    return out if status == 200 and out else None


async def _httpx_fetch(url: str, headers: dict[str, Any] | None,
                       max_time: int) -> bytes | None:
    try:
        r = await _get_client().get(url, headers=headers or {}, timeout=max_time)
    except Exception:
        return None
    if r.status_code != 200 or not r.content:
        return None
    body: bytes = r.content
    return body
