"""Тесты чат-API: реконсиляция (filter_clusters), маппинг chatId, отправка (без сети)."""
import json

from hrwork.application.apply.chat import chat


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeReq:
    """Заглушка Playwright APIRequestContext -> заранее заданные ответы на get/post."""
    def __init__(self, resp=None, post_resp=None):
        self._resp = resp
        self._post_resp = post_resp
        self.calls = []
        self.posts = []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        # для пагинации: список ответов -> по индексу вызова
        if isinstance(self._resp, list):
            i = min(len(self.calls) - 1, len(self._resp) - 1)
            return self._resp[i]
        return self._resp

    def post(self, url, headers=None, data=None):
        self.posts.append((url, headers, data))
        return self._post_resp


def test_parses_vacancy_ids():
    resp = _FakeResp(200, {"vacanciesClusters": [
        {"vacancyId": 121923039, "name": "Python-developer"},
        {"vacancyId": 124046929, "name": "Python LLM"},
    ]})
    ids = chat.applied_vacancy_ids(_FakeReq(resp), "xsrf")
    assert ids == {"121923039", "124046929"}      # id приведены к строке


def test_non_200_returns_empty():
    assert chat.applied_vacancy_ids(_FakeReq(_FakeResp(403, {})), "x") == set()


def test_missing_clusters_key():
    assert chat.applied_vacancy_ids(_FakeReq(_FakeResp(200, {})), "x") == set()


def test_sends_xsrf_header():
    req = _FakeReq(_FakeResp(200, {"vacanciesClusters": []}))
    chat.applied_vacancy_ids(req, "TOKEN123")
    _, headers = req.calls[0]
    assert headers["x-xsrftoken"] == "TOKEN123"


def _chats_page(items):
    return _FakeResp(200, {"chats": {"items": items, "page": 0, "perPage": 20}})


# ── list_chats: chatId + vacancyId + applicantId по страницам ──
def test_list_chats_collects_across_pages():
    p0 = _chats_page([
        {"id": 111, "currentParticipantId": "23015572", "resources": {"VACANCY": ["134809303"]}},
    ])
    p1 = _chats_page([])                            # пустая -> стоп
    chats = chat.list_chats(_FakeReq([p0, p1]), "x")
    assert chats == [{"chatId": 111, "vacancyId": "134809303", "applicantId": "23015572"}]


# ── deep_get: извлечение currentApplicantState на любой глубине (для autoclick) ──
def test_deep_get_returns_none_when_absent():
    assert chat.deep_get({"a": {"b": 1}}, "missing") is None


# ── find_chat: (chatId, applicantId) по vacancyId ──
def test_find_chat_returns_chat_and_applicant():
    items = [{"id": 5454131905, "currentParticipantId": 23015572,
              "resources": {"VACANCY": ["134516706"]}}]
    assert chat.find_chat(_FakeReq(_chats_page(items)), "x", "134516706") == (5454131905, 23015572)


def test_find_chat_none_when_absent():
    resp = [_chats_page([{"id": 1, "resources": {"VACANCY": ["999"]}}]), _chats_page([])]
    assert chat.find_chat(_FakeReq(resp), "x", "134516706") == (None, None)


# ── cover_message_id: слот сопроводительного = сообщение-отклик (workflowTransition+canEdit) ──
def _chat_data_with(items):
    return {"chat": {"messages": {"items": items}}}


def test_cover_message_id_prefers_workflow_transition():
    data = _chat_data_with([
        {"id": 14670788121, "workflowTransitionId": 1, "canEdit": True, "text": ""},
        {"id": 14670788745, "canEdit": True, "text": "письмо"},          # обычное сообщение
    ])
    assert chat.cover_message_id(data) == 14670788121


def test_cover_message_id_fallback_first_editable():
    data = _chat_data_with([{"id": 1, "canEdit": False}, {"id": 2, "canEdit": True}])
    assert chat.cover_message_id(data) == 2


def test_cover_message_id_none_when_no_editable():
    assert chat.cover_message_id(_chat_data_with([{"id": 1, "canEdit": False}])) is None
    assert chat.cover_message_id({}) is None


# ── save_cover: тело POST /save = {text, messageId} (правит слот, не шлёт новое) ──
def test_save_cover_posts_correct_body():
    req = _FakeReq(post_resp=_FakeResp(200, {}))
    ok = chat.save_cover(req, "TOK", 5454131905, 14670788121, "Здравствуйте!")
    assert ok is True
    url, headers, data = req.posts[0]
    assert url == chat.SAVE_URL
    assert json.loads(data) == {"text": "Здравствуйте!", "messageId": 14670788121}
    assert headers["x-xsrftoken"] == "TOK"


def test_save_cover_false_on_empty_or_error():
    assert chat.save_cover(_FakeReq(post_resp=_FakeResp(200, {})), "x", 1, None, "hi") is False
    assert chat.save_cover(_FakeReq(post_resp=_FakeResp(403, {})), "x", 1, 5, "hi") is False


# ── response_time: реальная дата отклика из сообщения-отклика ──
def test_response_time_prefers_workflow_transition():
    data = _chat_data_with([
        {"id": 1, "workflowTransitionId": 1, "creationTime": "2026-07-05T10:00:00+03:00"},
        {"id": 2, "creationTime": "2026-07-06T10:00:00+03:00"},
    ])
    assert chat.response_time(data) == "2026-07-05T10:00:00+03:00"


def test_response_time_fallback_earliest_message():
    data = _chat_data_with([
        {"id": 1, "creationTime": "2026-07-06T10:00:00+03:00"},
        {"id": 2, "creationTime": "2026-07-05T10:00:00+03:00"},
    ])
    assert chat.response_time(data) == "2026-07-05T10:00:00+03:00"


def test_response_time_empty_when_no_messages():
    assert chat.response_time({}) == ""
