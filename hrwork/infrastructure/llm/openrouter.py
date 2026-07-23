"""Тонкий синхронный транспорт к OpenRouter (OpenAI-совместимый chat/completions).

Синхронный httpx.post, НЕ async `net/http.py::fetch_bytes`: путь suggest -> propose
синхронный, тянуть async сюда неоправданно, а fetch_bytes заточен под сбор (bytes,
прокси, curl). Guarded import httpx (как cover.py с anthropic) — отсутствие пакета не
роняет импорт. ЛЮБАЯ ошибка/не-200/пустой ответ -> None: вызывающий откатывается на regex.
"""
from __future__ import annotations

import json

from hrwork.config import (
    INTENT_TIMEOUT,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    log,
)


def chat_json(system: str, user: str, *, model: str,
              timeout: float = INTENT_TIMEOUT, max_tokens: int = 30) -> str | None:
    """Один POST /chat/completions -> текст message.content, либо None.

    None при: нет httpx / нет ключа / не-200 / исключение / пустой ответ.
    temperature=0 (детерминизм), reasoning выключен (классификация рассуждений не требует,
    reasoning-токены оплачивались бы как output и добавляли латентность)."""
    if not OPENROUTER_API_KEY:
        return None
    try:
        import httpx
    except ImportError:
        log.debug("httpx не установлен — intent-классификатор недоступен")
        return None
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False},          # классификации reasoning не нужен
    }
    try:
        r = httpx.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
            content=json.dumps(payload), timeout=timeout,
        )
        if r.status_code != 200:
            log.warning("OpenRouter {} -> {}: {}", model, r.status_code, r.text[:200])
            return None
        content = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return content or None
    except Exception as e:                          # сеть/парсинг/структура — не роняем прогон
        log.warning("OpenRouter недоступен ({}) — откат на regex", e)
        return None
