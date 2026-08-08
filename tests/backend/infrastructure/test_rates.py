"""Тесты FX-сервиса: алиасы валют, TTL/кеш, фолбэк (без сети — мок фетча).

Отдельный предмет — вызов curl: он обязан вести себя так же, как боевой сетевой примитив
`net/http.py::_curl_fetch` (аудит 08.08.2026, находки 10 и 48). Курс валют питает ВСЕ
зарплатные срезы, и число, собранное из тела ошибки портала, хуже отсутствия курса —
фолбэков на этот случай ниже целых два (старый кеш, хардкод).
"""
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


# ── Вызов curl: разделитель и код ответа ────────────────────────────────────────────────

OK_BODY = b'{"result": "success", "rates": {"USD": 1.0, "EUR": 0.9, "RUB": 90.0}}'
#: тело ошибки провайдера, у которого поле `rates` в ответе ЕСТЬ (тарифный план исчерпан)
ERROR_BODY = b'{"result": "success", "rates": {"USD": 1.0, "RUB": 1.0}, "note": "quota"}'


class _Proc:
    """Ответ curl: тело в stdout, HTTP-код в stderr (`-w %{stderr}%{http_code}`)."""

    def __init__(self, stdout: bytes = OK_BODY, status: bytes = b"200") -> None:
        self.stdout, self.stderr = stdout, status


def _run(monkeypatch, proc):
    seen: dict[str, list[str]] = {}

    def fake_run(args, **_kw):
        seen["args"] = list(args)
        return proc

    monkeypatch.setattr(rates.subprocess, "run", fake_run)
    return seen


def test_successful_response_becomes_rates(monkeypatch):
    _run(monkeypatch, _Proc())
    assert rates._fetch() == {"USD": 1.0, "EUR": 0.9, "RUB": 90.0}


def test_url_goes_after_the_option_separator(monkeypatch):
    """Тот же контракт, что у `net/http.py::_curl_fetch`: адрес отделён `--`, иначе значение,
    начинающееся с дефиса, стало бы опцией curl."""
    seen = _run(monkeypatch, _Proc())
    rates._fetch()
    assert seen["args"][-2:] == ["--", rates.FX_API]


@pytest.mark.parametrize("status", [b"301", b"400", b"403", b"429", b"500", b"503"])
def test_error_body_is_never_parsed_as_rates(monkeypatch, status):
    # Тело не-200 не должно доехать до json.loads: курс из ответа об ошибке пересчитал бы
    # все зарплатные срезы, а отсутствие курса честно уходит в кеш/хардкод-фолбэк.
    _run(monkeypatch, _Proc(stdout=ERROR_BODY, status=status))
    assert rates._fetch() is None


def test_unreadable_status_is_treated_as_a_failure(monkeypatch):
    # curl не назвал код (сломался -w) -> считаем сбоем, а не успехом
    _run(monkeypatch, _Proc(stdout=OK_BODY, status=b"curl: unknown --write-out variable"))
    assert rates._fetch() is None


def test_broken_json_with_200_is_a_failure(monkeypatch):
    _run(monkeypatch, _Proc(stdout=b"<html>maintenance</html>"))
    assert rates._fetch() is None


def test_response_without_success_marker_is_a_failure(monkeypatch):
    _run(monkeypatch, _Proc(stdout=b'{"result": "error", "error-type": "invalid-key"}'))
    assert rates._fetch() is None
