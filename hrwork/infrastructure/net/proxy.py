"""Прокси для сбора данных с HH."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from hrwork.config import BASE_DIR

PROXY_FILE_CANDIDATES = ("proxie.txt", "proxy.txt")


@dataclass
class ProxyRotator:
    _items: list[str]
    _index: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self._items)

    @property
    def size(self) -> int:
        return len(self._items)

    def next_batch(self, limit: int) -> list[str]:
        if not self._items:
            return []
        count = max(1, min(limit, len(self._items)))
        out: list[str] = []
        for _ in range(count):
            out.append(self._items[self._index])
            self._index = (self._index + 1) % len(self._items)
        return out


def load_proxies(base_dir: Path = BASE_DIR) -> ProxyRotator:
    raw_disable = os.getenv("HH_DISABLE_PROXIES", "").strip().lower()
    if raw_disable in {"1", "true", "yes", "on"}:
        return ProxyRotator(_items=[])

    for name in PROXY_FILE_CANDIDATES:
        path = base_dir / name
        if path.exists():
            return ProxyRotator(_items=_read_proxy_lines(path))
    return ProxyRotator(_items=[])


def mask_proxy(proxy_url: str | None) -> str:
    if not proxy_url:
        return "direct"
    scheme, sep, rest = proxy_url.partition("://")
    if not sep:
        return "***"
    if "@" not in rest:
        return f"{scheme}://***"
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def _read_proxy_lines(path: Path) -> list[str]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_lines:
        parsed = _parse_proxy(raw)
        if not parsed or parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def _parse_proxy(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    if "://" in line:
        return line

    parts = [part.strip() for part in line.split(":")]
    if len(parts) == 2:
        host, port = parts
        if host and port.isdigit():
            return f"socks5://{host}:{port}"
        return None

    if len(parts) == 4:
        host, port, username, password = parts
        if not host or not port.isdigit():
            return None
        user_enc = quote(username, safe="")
        pass_enc = quote(password, safe="")
        return f"socks5://{user_enc}:{pass_enc}@{host}:{port}"

    return None
