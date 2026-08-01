"""Чат-API hh.ru (chatik.hh.ru) — cookie-only, БЕЗ Group-IB fingerprint.

В отличие от /applicant/resumes/touch и отклика (гейтятся fingerprintSp/x-gib-*),
чат-эндпоинты доступны по одним кукам сессии + x-xsrftoken. Используем для:
  - реконсиляции: список чатов = вакансии, на которые уже откликались (-> marks.json);
  - (перспективно) отправки сопроводительного письма сообщением в чат.

Работает через Playwright APIRequestContext (page.context.request) — он несёт куки
профиля. Чистого HTTP тут нет: куки живут в persistent-профиле.
"""
import contextlib
import json
import uuid
from typing import Any

CLUSTERS_URL = ("https://chatik.hh.ru/chatik/api/filter_clusters"
                "?filterUnread=false&filterHasTextMessage=false&do_not_track_session_events=true")
CHATS_URL = ("https://chatik.hh.ru/chatik/api/chats"
             "?filterUnread=false&filterHasTextMessage=false&do_not_track_session_events=true")
SAVE_URL = "https://chatik.hh.ru/chatik/api/save"   # правка текста сообщения (слот сопроводительного)
SEND_URL = ("https://chatik.hh.ru/chatik/api/send"  # НОВОЕ сообщение в чат
            "?hhtmSourceLabel=chat&hhtmSource=chat")
QUICK_REPLIES_URL = "https://chatik.hh.ru/chatik/api/quick_replies"
CHAT_DATA_URL = "https://chatik.hh.ru/chatik/api/chat_data"

# Статусы отклика (currentApplicantState в chat_data) -> подписи для ленты.
# Фактические значения короче перечня employerStatesClusters (напр. DISCARD, не
# DISCARD_BY_EMPLOYER) — держим оба варианта; незнакомый код покажется как есть.
STATE_LABELS = {
    "RESPONSE":               "Отклик",
    "INVITATION":             "Приглашение",
    "CONSIDER":               "Рассматривается",
    "PHONE_INTERVIEW":        "Телефон-интервью",
    "INTERVIEW":              "Интервью",
    "ASSESSMENT":             "Тестовое",
    "HIRED":                  "Оффер",
    "DISCARD":                "Отказ",
    "DISCARD_BY_EMPLOYER":    "Отказ",
    "DISCARD_BY_APPLICANT":   "Вы отказались",
    "DISCARD_VACANCY_CLOSED": "Вакансия закрыта",
}

# Наборы состояний — ЕДИНЫЙ источник для воронки (funnel) и инкремент-синка (autoclick):
# раньше каждый держал свой и наборы тихо разъезжались (fix.md №6).
DISCARD_STATES = frozenset({"DISCARD", "DISCARD_BY_EMPLOYER", "DISCARD_VACANCY_CLOSED"})
INVITED_STATES = frozenset({"INVITATION", "PHONE_INTERVIEW", "INTERVIEW",
                            "ASSESSMENT", "HIRED", "CONSIDER"})
# назад не флипаются -> кешевый статус вечен (наш собственный отказ — тоже терминал)
TERMINAL_STATES = DISCARD_STATES | {"DISCARD_BY_APPLICANT", "HIRED"}

def _headers(xsrf: str) -> dict[str, str]:
    return {
        "accept": "application/json",
        "x-xsrftoken": xsrf,
        "x-requested-with": "XMLHttpRequest",
        "x-hhtmsource": "app",
        "referer": "https://chatik.hh.ru/?platform=xhh&dest=iframe",
    }


def applied_vacancy_ids(request_ctx: Any, xsrf: str) -> set[str]:
    """ID вакансий, на которые есть чат (=есть отклик). Пусто при ошибке/не-200.
    request_ctx — Playwright APIRequestContext (page.context.request) с куками профиля."""
    with contextlib.suppress(Exception):
        r = request_ctx.get(CLUSTERS_URL, headers=_headers(xsrf))
        if r.status != 200:
            return set()
        data = r.json()
        return {str(c["vacancyId"])
                for c in (data.get("vacanciesClusters") or [])
                if c.get("vacancyId")}
    return set()


def _chat_vacancy_ids(item: dict[str, Any]) -> list[str]:
    """vacancyId(ы) чата: item.resources.VACANCY = ["134809303"]."""
    return [str(v) for v in ((item.get("resources") or {}).get("VACANCY") or [])]


def find_chat(request_ctx: Any, xsrf: str, vacancy_id: Any,
              pages: int = 3) -> tuple[Any, Any]:
    """(chatId, applicantId) для вакансии — из item.id и item.currentParticipantId.
    (None, None), если чат ещё не создан. applicantId нужен для chat_data/сопроводительного."""
    vid = str(vacancy_id)
    for page in range(pages):
        with contextlib.suppress(Exception):
            r = request_ctx.get(CHATS_URL + f"&page={page}", headers=_headers(xsrf))
            if r.status != 200:
                return None, None
            items = ((r.json().get("chats") or {}).get("items")) or []
            if not items:
                return None, None
            for it in items:
                if vid in _chat_vacancy_ids(it):
                    return it.get("id"), it.get("currentParticipantId")
    return None, None


def chat_data(request_ctx: Any, xsrf: str, chat_id: Any, applicant_id: Any) -> dict[str, Any]:
    """Полный chat_data (сообщения + состояние) или {} при ошибке. Cookie-only."""
    url = (f"{CHAT_DATA_URL}?chatId={chat_id}&applicantId={applicant_id}"
           f"&do_not_track_session_events=true")
    with contextlib.suppress(Exception):
        r = request_ctx.get(url, headers=_headers(xsrf))
        if r.status == 200:
            data: dict[str, Any] = r.json()
            return data
    return {}


def chat_entry(chat_id: Any, data: dict[str, Any]) -> dict[str, Any]:
    """chat_data -> запись для chat_messages.json: {chatId, write, messages}.
    Формат ДОЛЖЕН совпадать с инлайн-сборкой в autoclick.sync_statuses (там она не
    вынесена сюда намеренно — боевой путь откликов не рефакторим ради этого). Общий
    потребитель — chat_reply.poll_replies (точечный опрос после отправки)."""
    ch = data.get("chat") or {}
    items = ((ch.get("messages") or {}).get("items")) or []
    return {
        "chatId": chat_id,
        "write": ch.get("writePossibility") or {},
        # type != SIMPLE — служебные («рекрутер присоединился»), в переписку не идут
        "messages": [{"text": m.get("text") or "", "mine": bool(m.get("canEdit")),
                      "ts": m.get("creationTime") or "",
                      "bot": bool((m.get("participantDisplay") or {}).get("isBot"))}
                     for m in items
                     if (m.get("text") or "").strip() and m.get("type") == "SIMPLE"],
    }


def cover_message_id(data: dict[str, Any]) -> Any:
    """id сообщения-отклика = слот сопроводительного. Это сообщение с workflowTransition
    (событие отклика), редактируемое (canEdit). Фолбэк — первое редактируемое. None — нет."""
    items = (((data.get("chat") or {}).get("messages") or {}).get("items")) or []
    for m in items:
        if (m.get("workflowTransitionId") or m.get("workflowTransition")) and m.get("canEdit"):
            return m.get("id")
    for m in items:
        if m.get("canEdit"):
            return m.get("id")
    return None


def response_time(data: dict[str, Any]) -> str:
    """creationTime сообщения-отклика (workflowTransition), иначе самого раннего сообщения.
    Это РЕАЛЬНАЯ дата отклика — в т.ч. сделанного руками на hh.ru (любой отклик = чат).
    Пустая строка, если сообщений нет."""
    items = (((data.get("chat") or {}).get("messages") or {}).get("items")) or []
    resp = next((m for m in items if m.get("workflowTransitionId") or m.get("workflowTransition")), None)
    if resp is None and items:
        resp = min(items, key=lambda m: m.get("creationTime") or "")
    ts: str = (resp or {}).get("creationTime") or ""
    return ts


def save_cover(request_ctx: Any, xsrf: str, chat_id: Any, message_id: Any, text: str) -> bool:
    """POST /save: {text, messageId} — записать письмо в СЛОТ СОПРОВОДИТЕЛЬНОГО (правит
    сообщение-отклик, а не шлёт новое). True при 2xx. Cookie-only, без fingerprint."""
    if not (message_id and text):
        return False
    headers = {
        **_headers(xsrf),
        "content-type": "application/json",
        "origin": "https://chatik.hh.ru",
        "referer": f"https://chatik.hh.ru/chat/{chat_id}",
    }
    payload = {"text": text, "messageId": int(message_id)}
    with contextlib.suppress(Exception):
        r = request_ctx.post(SAVE_URL, headers=headers, data=json.dumps(payload))
        return bool(200 <= r.status < 300)
    return False


def send_message(request_ctx: Any, xsrf: str, chat_id: Any, text: str, *,
                 vacancy_url: str = "", idempotency_key: str | None = None) -> bool:
    """Отправить НОВОЕ сообщение в чат. True при 2xx.

    Cookie-only: гейта Group-IB (x-gib-*) здесь нет — в отличие от отклика и /resumes/touch,
    поэтому работает из обычного HTTP-клиента, браузер не нужен.

    `idempotencyKey` (UUID) — защита от дублей на стороне HH: повтор с тем же ключом не создаст
    второе сообщение. Генерируем на каждый ВЫЗОВ, а ретрай должен переиспользовать тот же ключ.

    `metadata.source` НЕ проставляем: в браузере он помечает шаблон быстрого ответа
    (`quick_reply` + template_id). Наш текст шаблоном не является — врать в телеметрии не нужно.
    """
    if not (chat_id and (text or "").strip()):
        return False
    headers = {
        **_headers(xsrf),
        "content-type": "application/json",
        "origin": "https://chatik.hh.ru",
        "referer": f"https://chatik.hh.ru/chat/{chat_id}",
        "x-hhtmfrom": "vacancy",
        "x-hhtmfromlabel": "vacancy",
        "x-hhtmsourcelabel": "vacancy",
    }
    payload = {
        "chatId": int(chat_id),
        "idempotencyKey": idempotency_key or str(uuid.uuid4()),
        "text": text,
        "metadata": {
            "chatType": "NEGOTIATION",
            "parentWindowUrl": vacancy_url or "https://hh.ru/",
            "location": "chatik",
            "platform": {"name": "xhh", "data": {}},
        },
    }
    with contextlib.suppress(Exception):
        r = request_ctx.post(SEND_URL, headers=headers, data=json.dumps(payload))
        return bool(200 <= r.status < 300)
    return False


def quick_replies(request_ctx: Any, xsrf: str, chat_id: Any,
                  message_id: Any) -> list[dict[str, Any]]:
    """Подсказки HH для ответа. ВНИМАНИЕ: это вопросы СОИСКАТЕЛЯ работодателю
    («Какой график работы?»), а не варианты ответа на его вопрос — на содержательные
    вопросы список приходит пустым. Держим для полноты API."""
    url = (f"{QUICK_REPLIES_URL}?chatId={chat_id}&messageId={message_id}"
           f"&do_not_track_session_events=true")
    with contextlib.suppress(Exception):
        r = request_ctx.get(url, headers=_headers(xsrf))
        if r.status == 200:
            replies: list[dict[str, Any]] = (r.json() or {}).get("quick_replies") or []
            return replies
    return []


def list_chats(request_ctx: Any, xsrf: str, pages: int = 30) -> list[dict[str, Any]]:
    """Все чаты постранично -> [{chatId, vacancyId, applicantId, lastMessageTime}].
    Пагинация до пустой страницы (или `pages` максимум). Cookie-only.
    lastMessageTime (ISO с tz) — creationTime последнего сообщения из самого списка:
    по нему инкрементальный синк решает «качать или взять из кеша» без запроса в чат.
    НЕ lastActivityTime: то поле обновляет в том числе НАШЕ чтение chat_data (замер
    23.07: после полного синка 84 % чатов «активны за сутки») — самоотравляющийся сигнал."""
    out: list[dict[str, Any]] = []
    for page in range(pages):
        got = False
        with contextlib.suppress(Exception):
            r = request_ctx.get(CHATS_URL + f"&page={page}", headers=_headers(xsrf))
            if r.status != 200:
                break
            items = ((r.json().get("chats") or {}).get("items")) or []
            if not items:
                break
            got = True
            for it in items:
                vac = _chat_vacancy_ids(it)
                if vac:
                    out.append({"chatId": it.get("id"), "vacancyId": vac[0],
                                "applicantId": it.get("currentParticipantId"),
                                "lastMessageTime":
                                    ((it.get("lastMessage") or {}).get("creationTime")) or ""})
        if not got:
            break
    return out


def deep_get(obj: Any, key: str) -> Any:
    """Первое значение по ключу на любой глубине (для currentApplicantState в chat_data)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = deep_get(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = deep_get(x, key)
            if r is not None:
                return r
    return None


