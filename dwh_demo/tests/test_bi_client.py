"""Логика фасада MetabaseClient — без HTTP: подменяем низкоуровневый _api.

Проверяем то, что раньше копипастилось в 5 скриптах: аутентификацию,
идемпотентный upsert дашборда (архивация старых карточек) и обработку ошибок.
"""
import pytest

from bi.client import MetabaseClient

pytestmark = pytest.mark.unit


def make_client():
    return MetabaseClient("http://mb", "demo@hh.local", "pw")


# ── аутентификация ──
def test_connect_logs_in_when_no_setup_token():
    c = make_client()
    seen = []

    def fake(method, path, data=None):
        seen.append((method, path))
        if path == "/api/session/properties":
            return 200, {"setup-token": None}     # инстанс уже настроен
        if path == "/api/session":
            return 200, {"id": "tok-123"}
        return 200, None

    c._api = fake
    c.connect()
    assert c.token == "tok-123"
    assert ("POST", "/api/session") in seen


def test_connect_raises_on_auth_failure():
    c = make_client()

    def fake(method, path, data=None):
        if path == "/api/session/properties":
            return 200, {}
        return 401, {"errors": {"password": "bad"}}

    c._api = fake
    with pytest.raises(RuntimeError):
        c.connect()


# ── карточки ──
def test_create_card_returns_id():
    c = make_client()
    c.token = "t"

    def fake(method, path, data=None):
        assert (method, path) == ("POST", "/api/card")
        return 200, {"id": 42}

    c._api = fake
    assert c.create_card("Навыки", 1, "SELECT 1", "scalar") == 42


def test_create_card_raises_on_error():
    c = make_client()
    c.token = "t"
    c._api = lambda *a, **k: (400, "boom")
    with pytest.raises(RuntimeError):
        c.create_card("bad", 1, "SELECT oops", "scalar")


# ── идемпотентный upsert дашборда ──
def test_upsert_dashboard_archives_existing_cards():
    c = make_client()
    c.token = "t"
    archived = []

    def fake(method, path, data=None):
        if method == "GET" and path == "/api/dashboard":
            return 200, [{"id": 7, "name": "Demo"}]            # уже существует
        if method == "GET" and path == "/api/dashboard/7":
            return 200, {"dashcards": [{"card_id": 11}, {"card_id": 12}]}
        if method == "PUT" and path.startswith("/api/card/"):
            archived.append(path)
            return 200, None
        if method == "PUT" and path == "/api/dashboard/7":
            return 200, None
        return 200, None

    c._api = fake
    assert c.upsert_dashboard("Demo") == 7
    assert archived == ["/api/card/11", "/api/card/12"]        # старые карточки заархивированы


def test_upsert_dashboard_creates_when_absent():
    c = make_client()
    c.token = "t"

    def fake(method, path, data=None):
        if method == "GET" and path == "/api/dashboard":
            return 200, []                                     # дашборда нет
        if method == "POST" and path == "/api/dashboard":
            return 200, {"id": 99}
        return 200, None

    c._api = fake
    assert c.upsert_dashboard("New") == 99
