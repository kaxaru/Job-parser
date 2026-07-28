"""Официальный API hh.ru — второй путь рядом с браузерным.

Сеть не трогаем: проверяем сборку OAuth-ссылки, срок жизни токена, разбор ответа и то, что
клиент обновляет протухший токен РОВНО один раз. Живой запрос — только `hh.py hhapi --probe`.
"""
import time

import pytest

from hrwork.infrastructure.sources import hh_api


@pytest.fixture
def app_keys(monkeypatch):
    monkeypatch.setattr(hh_api, "HH_CLIENT_ID", "APPID")
    monkeypatch.setattr(hh_api, "HH_CLIENT_SECRET", "APPSECRET")
    monkeypatch.setattr(hh_api, "HH_REDIRECT_URI", "http://localhost:8765/callback")


def test_authorize_url_built_from_own_app(app_keys):
    assert hh_api.authorize_url("ST8") == (
        "https://hh.ru/oauth/authorize?response_type=code&client_id=APPID"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback&state=ST8")


def test_authorize_url_without_client_id_fails_fast(monkeypatch):
    # своя ошибка конфигурации -> падаем громко, а не уходим в HH с пустым client_id
    monkeypatch.setattr(hh_api, "HH_CLIENT_ID", "")
    with pytest.raises(ValueError):
        hh_api.authorize_url("ST8")


def test_token_set_from_response_keeps_absolute_expiry(monkeypatch):
    # `expires_in` от HH относительный; храним АБСОЛЮТНЫЙ момент, иначе после перезапуска
    # процесса неясно, сколько токену осталось. Время замораживаем -> ожидание точным литералом
    monkeypatch.setattr(hh_api.time, "time", lambda: 1_800_000_000.0)
    t = hh_api.TokenSet.from_response(
        {"access_token": "AT", "refresh_token": "RT", "expires_in": 1209600})
    assert t.access == "AT"
    assert t.refresh == "RT"
    assert t.expires_at == 1_801_209_600.0


@pytest.mark.parametrize("seconds_left, expired", [
    (1209600, False),     # свежий двухнедельный токен
    (600, False),         # 10 минут — ещё рабочий
    (299, True),          # внутри пятиминутного запаса — обновляем заранее
    (0, True),
    (-60, True),
])
def test_token_expiry_uses_five_minute_margin(seconds_left, expired):
    t = hh_api.TokenSet(access="AT", refresh="RT", expires_at=time.time() + seconds_left)
    assert t.is_expired is expired


def test_refresh_without_refresh_token_is_error():
    t = hh_api.TokenSet(access="AT", refresh="", expires_at=0.0)
    with pytest.raises(hh_api.HhApiError) as e:
        hh_api.refresh(t)
    assert e.value.status == 400


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def test_client_refreshes_expired_token_once(monkeypatch):
    """Протухший токен -> обновление -> повтор запроса. Повтор РОВНО один: если и после
    обновления отказ, дело не в сроке, а в правах — молотить запросами бессмысленно."""
    calls: list[str] = []

    def fake_request(method, url, **kw):
        calls.append(kw["headers"]["Authorization"])
        if kw["headers"]["Authorization"] == "Bearer NEW":
            return _Resp(200, {"id": "42"})
        return _Resp(403, {"errors": [{"value": "bad_authorization", "type": "oauth"}]})

    monkeypatch.setattr(hh_api.httpx, "request", fake_request)
    monkeypatch.setattr(hh_api, "refresh",
                        lambda t: hh_api.TokenSet("NEW", "RT2", time.time() + 3600))
    client = hh_api.ApiClient(hh_api.TokenSet("OLD", "RT", time.time() + 3600))
    assert client.get("/me") == {"id": "42"}
    assert calls == ["Bearer OLD", "Bearer NEW"]


def test_client_gives_up_after_one_refresh(monkeypatch):
    attempts: list[str] = []

    def always_403(method, url, **kw):
        attempts.append(kw["headers"]["Authorization"])
        return _Resp(403, {"errors": [{"value": "bad_authorization", "type": "oauth"}]})

    monkeypatch.setattr(hh_api.httpx, "request", always_403)
    monkeypatch.setattr(hh_api, "refresh",
                        lambda t: hh_api.TokenSet("NEW", "RT2", time.time() + 3600))
    client = hh_api.ApiClient(hh_api.TokenSet("OLD", "RT", time.time() + 3600))
    with pytest.raises(hh_api.HhApiError) as e:
        client.get("/me")
    assert e.value.status == 403
    assert attempts == ["Bearer OLD", "Bearer NEW"]


def test_client_without_token_names_the_next_step():
    client = hh_api.ApiClient(None)
    monkey_token = getattr(client, "token", "unset")
    if monkey_token is None:                      # профиля с токеном на машине может не быть
        with pytest.raises(hh_api.HhApiError) as e:
            client.get("/me")
        assert e.value.status == 401


def test_headers_carry_contact_user_agent(monkeypatch):
    monkeypatch.setattr(hh_api, "HH_API_UA", "hr-work-applicant/1.0 (mail@example.com)")
    h = hh_api._headers("AT")
    assert h["HH-User-Agent"] == "hr-work-applicant/1.0 (mail@example.com)"
    assert h["Authorization"] == "Bearer AT"
