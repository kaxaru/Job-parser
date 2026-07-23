"""Транспорт OpenRouter: главное свойство — ЛЮБАЯ проблема деградирует в None (fallback),
сеть замокана, реальных вызовов нет."""
import json

import pytest

from hrwork.infrastructure.llm import openrouter


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if isinstance(body, dict) else str(body)

    def json(self):
        return self._body


def _ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


def _patch_key(monkeypatch, key="test-key"):
    monkeypatch.setattr(openrouter, "OPENROUTER_API_KEY", key)


def test_returns_content_on_200(monkeypatch):
    _patch_key(monkeypatch)
    captured = {}

    def fake_post(url, headers, content, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json.loads(content)
        return _ok('{"intent":"years","tech":[]}')

    monkeypatch.setattr("httpx.post", fake_post)
    out = openrouter.chat_json("sys", "вопрос", model="m")
    assert out == '{"intent":"years","tech":[]}'
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["payload"]["temperature"] == 0
    assert captured["payload"]["model"] == "m"
    assert captured["payload"]["messages"][1]["content"] == "вопрос"


def test_no_key_no_network(monkeypatch):
    _patch_key(monkeypatch, "")
    called = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: called.append(1))
    assert openrouter.chat_json("s", "u", model="m") is None
    assert called == []                          # сеть НЕ дёрнута


@pytest.mark.parametrize("resp", [
    _Resp(429, {"error": "rate"}),
    _Resp(500, "boom"),
    _Resp(200, {"choices": [{"message": {"content": ""}}]}),   # пустой content
])
def test_bad_response_returns_none(monkeypatch, resp):
    _patch_key(monkeypatch)
    monkeypatch.setattr("httpx.post", lambda *a, **k: resp)
    assert openrouter.chat_json("s", "u", model="m") is None


def test_exception_returns_none(monkeypatch):
    _patch_key(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("httpx.post", boom)
    assert openrouter.chat_json("s", "u", model="m") is None
