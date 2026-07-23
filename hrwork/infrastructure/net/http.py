"""Сетевой примитив сбора: один GET -> bytes|None. Бэкенд выбирается config.HTTP_BACKEND.

- 'curl' (дефолт): отдельный процесс curl на запрос. Стабилен на Windows+VPN, где aiohttp падал
  с WinError 64; минус — спавн процесса + новый TLS на каждый запрос (потолок скорости сбора).
- 'httpx': ОДИН пулированный AsyncClient (keep-alive переиспользует TCP/TLS) — кратно быстрее на
  беспроксишном пути (hirify, HH без прокси). Проксированные запросы (ротация IP у HH) пул не
  умеет -> для них откат на curl автоматически. Экспериментальный флаг, A/B на своей сети.

Ретраи/бэкофф и валидацию контента делают вызывающие (у HH и hirify они осознанно разные).
"""
import asyncio
import shutil

from hrwork.config import CURL_MAX_TIME, HTTP_BACKEND, log

CURL = shutil.which("curl") or "curl"
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


def _get_client():
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


async def fetch_bytes(url: str, *, headers: dict | None = None, proxy: str | None = None,
                      max_time: int = CURL_MAX_TIME) -> bytes | None:
    """GET url -> сырые bytes (или None при сбое / не-200 / пустом ответе). headers/proxy —
    структурно (бэкенд сам переведёт). Проксированный запрос под httpx-флагом идёт через curl."""
    if _BACKEND == "httpx" and proxy is None:
        return await _httpx_fetch(url, headers, max_time)
    return await _curl_fetch(url, headers, proxy, max_time)


async def _curl_fetch(url, headers, proxy, max_time) -> bytes | None:
    args = [CURL, "-s", "--compressed", "--max-time", str(max_time)]
    for k, v in (headers or {}).items():
        args += (["-A", v] if k.lower() == "user-agent" else ["-H", f"{k}: {v}"])
    if proxy:
        args += ["--proxy", proxy]
    args.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    except Exception:
        return None
    if proc.returncode != 0 or not out:
        return None
    return out


async def _httpx_fetch(url, headers, max_time) -> bytes | None:
    try:
        r = await _get_client().get(url, headers=headers or {}, timeout=max_time)
    except Exception:
        return None
    if r.status_code != 200 or not r.content:
        return None
    return r.content
