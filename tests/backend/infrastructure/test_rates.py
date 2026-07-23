"""Тесты FX-сервиса: алиасы валют, TTL/кеш, фолбэк (без сети — мок фетча)."""
import json
import time

import pytest

from hrwork.infrastructure.net import rates


def test_resolve_currency_aliases():
    assert rates.resolve_currency("RUR") == "RUB"
    assert rates.resolve_currency("USDT") == "USD"
    assert rates.resolve_currency("byr") == "BYN"
    assert rates.resolve_currency("EUR") == "EUR"
    assert rates.resolve_currency("") == ""


@pytest.fixture
def tmp_fx(tmp_path, monkeypatch):
    monkeypatch.setattr(rates, "FX_CACHE_FILE", tmp_path / "fx_rates.json")
    monkeypatch.setattr(rates, "DATA_DIR", tmp_path)
    return tmp_path / "fx_rates.json"


def test_fetch_success_writes_cache(tmp_fx, monkeypatch):
    monkeypatch.setattr(rates, "_fetch", lambda: {"USD": 1.0, "EUR": 0.9, "RUB": 90.0})
    r = rates.get_rates()
    assert r["RUB"] == 90.0
    assert tmp_fx.exists()
    cached = json.loads(tmp_fx.read_text(encoding="utf-8"))
    assert "fetched_at" in cached and cached["rates"]["EUR"] == 0.9


def test_fresh_cache_skips_fetch(tmp_fx, monkeypatch):
    tmp_fx.write_text(json.dumps({"fetched_at": time.time(), "rates": {"USD": 1.0, "RUB": 80.0}}),
                      encoding="utf-8")

    def _boom():
        raise AssertionError("свежий кеш -> фетчить не должны")
    monkeypatch.setattr(rates, "_fetch", _boom)
    assert rates.get_rates()["RUB"] == 80.0     # из свежего кеша


def test_fetch_fail_falls_back_to_hardcode(tmp_fx, monkeypatch):
    monkeypatch.setattr(rates, "_fetch", lambda: None)
    r = rates.get_rates()                        # ни API, ни кеша -> фолбэк
    assert r["USD"] == 1.0 and "RUB" in r
