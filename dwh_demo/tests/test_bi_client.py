"""Логика фасада MetabaseClient — без HTTP: подменяем низкоуровневый _api.

Проверяем то, что раньше копипастилось в 5 скриптах: аутентификацию, идемпотентные
подключения к движкам (`ensure_database`) и upsert дашборда (архивация старых карточек),
а также сообщения об ошибках — фасад обязан назвать статус и виновника, а не падать
где-то ниже на `str.get`.

Подмена приватного `_api` — осознанный шов «фасад без HTTP»: наблюдаемый контракт здесь
именно последовательность обращений к чужому API (Metabase), а не внутреннее поле.
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
    # Ровно два обращения: /api/setup НЕ зовётся, когда setup-token пуст.
    assert seen == [("GET", "/api/session/properties"), ("POST", "/api/session")]


def test_connect_raises_on_auth_failure():
    c = make_client()

    def fake(method, path, data=None):
        if path == "/api/session/properties":
            return 200, {}
        return 401, {"errors": {"password": "bad"}}

    c._api = fake
    with pytest.raises(RuntimeError, match=r"Metabase auth failed: 401"):
        c.connect()


# ── карточки ──
@pytest.mark.parametrize("status", [200, 202])
def test_create_card_returns_id(status):
    """202 — асинхронное создание карточки в Metabase, такой же успех, как 200."""
    c = make_client()
    c.token = "t"

    def fake(method, path, data=None):
        assert (method, path) == ("POST", "/api/card")
        return status, {"id": 42}

    c._api = fake
    assert c.create_card("Навыки", 1, "SELECT 1", "scalar") == 42


def test_create_card_raises_on_error():
    c = make_client()
    c.token = "t"
    c._api = lambda *a, **k: (400, "boom")
    with pytest.raises(RuntimeError, match=r"card 'bad' failed: 400"):
        c.create_card("bad", 1, "SELECT oops", "scalar")


# ── подключения к движкам: идемпотентность ──
# Metabase отдаёт список БД в двух формах — обёрнутой и голой; обе означают одно и то же.
@pytest.mark.parametrize("listing", [
    {"data": [{"id": 3, "name": "HH DWH", "engine": "postgres"}]},
    [{"id": 3, "name": "HH DWH", "engine": "postgres"}],
])
def test_existing_database_connection_is_reused(listing):
    c = make_client()
    c.token = "t"
    posted = []

    def fake(method, path, data=None):
        if (method, path) == ("GET", "/api/database"):
            return 200, listing
        posted.append(path)
        return 200, {"id": 999}

    c._api = fake
    assert c.ensure_database("HH DWH", "postgres", {}) == 3
    assert posted == []                        # повторный провижининг не создаёт дубль


def test_absent_database_is_created_and_synced():
    c = make_client()
    c.token = "t"
    posted = []

    def fake(method, path, data=None):
        if (method, path) == ("GET", "/api/database"):
            return 200, {"data": []}
        posted.append(path)
        return 200, {"id": 8}

    c._api = fake
    assert c.ensure_database("HH ClickHouse", "clickhouse", {}) == 8
    assert posted == ["/api/database", "/api/database/8/sync_schema"]


def test_database_of_another_engine_is_not_reused():
    """Имя совпало, движок нет — это другое подключение, переиспользовать нельзя."""
    c = make_client()
    c.token = "t"
    posted = []

    def fake(method, path, data=None):
        if (method, path) == ("GET", "/api/database"):
            return 200, {"data": [{"id": 3, "name": "HH DWH", "engine": "h2"}]}
        posted.append(path)
        return 200, {"id": 4}

    c._api = fake
    assert c.ensure_database("HH DWH", "postgres", {}) == 4
    assert posted == ["/api/database", "/api/database/4/sync_schema"]


def test_database_list_error_names_status_and_body():
    # Регрессия 09.08.2026: тело ошибки приходит СТРОКОЙ, и оно молча уезжало в d.get(...) —
    # провижининг падал «AttributeError: 'str' object has no attribute 'get'».
    c = make_client()
    c.token = "t"
    c._api = lambda *a, **k: (500, "gateway boom")
    with pytest.raises(RuntimeError, match=r"database list failed: 500 gateway boom"):
        c.ensure_database("HH DWH", "postgres", {})


def test_database_create_error_names_the_connection():
    c = make_client()
    c.token = "t"

    def fake(method, path, data=None):
        if (method, path) == ("GET", "/api/database"):
            return 200, {"data": []}
        return 400, "engine unknown"

    c._api = fake
    with pytest.raises(RuntimeError, match=r"database 'HH MSSQL' failed: 400"):
        c.ensure_database("HH MSSQL", "sqlserver", {})


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
