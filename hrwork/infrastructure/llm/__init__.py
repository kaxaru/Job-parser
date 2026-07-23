"""LLM-транспорт (OpenRouter, OpenAI-совместимый). Опциональная внешняя зависимость —
как cover.py с anthropic, любая ошибка деградирует в None, вызывающий откатывается."""
from hrwork.infrastructure.llm.openrouter import chat_json

__all__ = ["chat_json"]
