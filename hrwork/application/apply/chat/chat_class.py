"""Классификация переписки в чатах HH: что ответил работодатель и ждёт ли он ответа.

Признаки собраны по живой выборке 600 чатов (19.07): 683 сообщения работодателей, из них
51% — дословные повторы (машинная рассылка). Распределение: отказ 30%, приглашение 25%,
живой текст 17%, бот-скрининг 14%, прямой вопрос 13%.

Чистая логика над текстом (без сети) — тестируется напрямую.
"""
import re
from enum import Enum


class ChatKind(Enum):
    """Тип последнего сообщения работодателя (для бейджа в ленте)."""
    QUESTION = "question"      # прямой вопрос — нужен ответ
    SCREENING = "screening"    # бот зовёт на внешний скрининг/анкету
    REDIRECT = "redirect"      # перевод в другой канал (telegram и т.п.) — не отвечается
    INVITE = "invite"          # приглашение/интерес
    REJECT = "reject"          # отказ
    OTHER = "other"            # живой текст без явного вопроса
    ACK = "ack"                # заглушка «резюме получено, свяжемся» — тупик/фриз, ответа не ждёт
    NONE = "none"              # работодатель ещё не отвечал

    @property
    def code(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def needs_reply(self) -> bool:
        """Ждёт нашей реакции? Отказ и молчание — не ждут. REDIRECT ждёт — но реакция
        «пойти по ссылке», а не «написать в чат» (в чат отвечать бессмысленно)."""
        return self in (ChatKind.QUESTION, ChatKind.SCREENING, ChatKind.REDIRECT,
                        ChatKind.INVITE, ChatKind.OTHER, ChatKind.ACK)


_LABELS = {
    ChatKind.QUESTION:  "❓ вопрос",
    ChatKind.SCREENING: "🤖 анкета",
    ChatKind.REDIRECT:  "↗ внешний канал",
    ChatKind.INVITE:    "🎉 приглашение",
    ChatKind.REJECT:    "✖ отказ",
    ChatKind.OTHER:     "💬 сообщение",
    ChatKind.ACK:       "🧊 фриз",
    ChatKind.NONE:      "",
}

# Порядок проверок важен: бот-скрининг и отказ распознаём ДО вопроса — они тоже
# бывают с «?», но отвечать в чат бессмысленно (скрининг уводит на внешнюю форму).
_SCREENING = re.compile(
    r"первичн\w*\s+интервью|гигарекрутер|пройдите\s+(?:короткое|опрос)|"
    r"чтобы работодатель узнал|ответьте на (?:несколько )?вопрос|"
    r"не забудьте пройти|пройти отбор|видеоинтервью|тестовое задание по ссылке|"
    # «заполните анкету» — тот же внешний скрининг, только словами побуждения
    r"заполн\w+\s+(?:небольш\w+\s+|пожалуйста,?\s+)?анкет|заполнени\w+\s+анкет", re.I)
# Боты часто спрашивают ПОВЕЛИТЕЛЬНЫМ наклонением, без «?»: «Укажите ваши зарплатные
# ожидания…», «Расскажите про опыт…». Проверка только по «?» такие пропускала.
_IMPERATIVE_ASK = re.compile(
    r"\b(?:укажите|расскажите|напишите|уточните|опишите|сообщите|поделитесь)\b", re.I)
_REJECT = re.compile(
    r"к сожалению|не готовы пригласить|отказ|не подходит|другого кандидата|"
    r"вакансия закрыта|уже закрыли эту позицию", re.I)
_INVITE = re.compile(
    r"приглаша\w+|готовы предложить|назначим|созвон|собеседовани\w*\s+(?:в|на)|"
    r"когда вам удобно|буд(?:у|ем) рад обсудить", re.I)
# Заглушка-подтверждение: «резюме получено, изучим/рассмотрим, свяжемся, благодарим за интерес».
# Тупик — ответа не требует, но висит неделями. Помечаем «фризом», чтобы отличать от живых
# вопросов в ленте. Проверяется ПОСЛЕ вопроса — «резюме получено, а сколько лет опыта?» = вопрос.
_ACK = re.compile(
    r"резюме\s+(?:получен|принят)|получили\s+ваш\w*\s+резюме|"
    r"свяжемся\s+с\s+вами|вернёмся\s+к\s+вам|дад(?:им|ут)\s+обратную\s+связь|"
    r"благодар\w+\s+за\s+(?:проявленный\s+)?интерес", re.I)

# ── Темы вопроса: ЕДИНЫЙ источник для бейджа «решай сам» и роутинга chat_answer ──
# Раньше здесь была своя пара регэкспов (_SALARY/_RELOC), а в chat_answer — своя,
# и дыры чинились только в одной: «заработной платы» не ловилось «зарплат»,
# «готовы работать в г. Шатура» — «переезд» (найдено на живых чатах 20.07).
SALARY_Q = re.compile(
    r"вилк|зарплат|заработн\w*\s+плат|оклад|на руки|доход|\bз/п|\bз\.п|сколько хотите"
    r"|ожидани\w*\s+по\s+(?:деньга|доход|оплат)"
    r"|salary|compensation|expected pay|day rate|hourly rate|how much do you", re.I)
PLACE_Q = re.compile(
    r"переезд|релокац|готовы (?:ли )?(?:работать|переехать)\s+в\s+г"
    r"|готовы на фултайм в офис|переехать"
    # ЗАКРЫТЫЙ вопрос про конкретный офис: «готовы к офисному формату?», «очный формат
    # из офиса в СПб — вы готовы?», «удобно ли добираться до офиса». Ответ «работаю
    # удалённо» здесь = отказ от вакансии, то есть переговоры -> человеку. Открытое
    # «какой формат работы вам подходит?» остаётся за _FORMAT (dry-run 20.07).
    r"|готовы(?:\s+ли)?[^?]{0,40}(?:офис|очн\w*\s+формат|гибрид)"
    r"|(?:до|из|в)\s+офис\w*|офисн\w*\s+формат|очн\w*\s+формат"
    r"|relocat|willing to move|ready to move|work from our office"
    r"|(?:from|in)\s+(?:the\s+)?office|on.?site\s+(?:work|role|position)", re.I)

# Перевод в другой канал: телеграм/мессенджер, «для связи напишите…». Отвечать в чат
# бессмысленно by design — человек сам решает, идти ли по ссылке. Ловит «вопросы»
# в повелительном наклонении, которые раньше засоряли QUESTION (11 из 19 «молчащих»).
# @handle с lookbehind: без него «hr@mail.ru» превращал письмо с почтой в redirect.
# Проверяется ПОСЛЕ reject: отказ со ссылкой на телеграм-канал вакансий — всё равно отказ.
_REDIRECT = re.compile(
    r"t\.me/|телеграм|telegram|whatsapp|вотсап|(?<![\w.@])@[a-z0-9_]{4,}\b"
    r"|для связи (?:напишите|свяжитесь)|напишите нам в|свяжитесь с нами в", re.I)


def classify(text: str) -> ChatKind:
    """Тип одного сообщения работодателя."""
    t = " ".join((text or "").split())
    if not t:
        return ChatKind.NONE
    if _SCREENING.search(t):
        return ChatKind.SCREENING
    if _REJECT.search(t):
        return ChatKind.REJECT
    if _REDIRECT.search(t):
        return ChatKind.REDIRECT
    if _INVITE.search(t):
        return ChatKind.INVITE
    if "?" in t or _IMPERATIVE_ASK.search(t):
        return ChatKind.QUESTION
    if _ACK.search(t):
        return ChatKind.ACK                    # «резюме получено, свяжемся» — фриз, не вопрос
    return ChatKind.OTHER


def is_manual_only(text: str) -> bool:
    """Вопрос про деньги или место работы: даже с готовым ответом движка решение
    за человеком — подсвечиваем бейджем «решай сам»."""
    t = " ".join((text or "").split())
    return bool(SALARY_Q.search(t) or PLACE_Q.search(t))


# ── Кто написал: бот / шаблонная рассылка / живой человек ──
# Одного признака НЕ хватает, проверено на 93 ждущих чатах:
#   * `isBot` из API ловит ботов HH («ИИ-помощник») и «Робот-рекрутер» — 22 чата,
#     но ПРОПУСКАЕТ шаблоны, отправленные от имени живого рекрутера («Борисов Вячеслав»);
#   * повтор текста по корпусу ловит любую рассылку — 48 чатов, но пропускает шаблон,
#     отправленный ВПЕРВЫЕ (опознается задним числом, когда придёт второй такой же).
# Поэтому используем оба: is_bot ИЛИ is_template. Ошибаться безопаснее в сторону «личное»
# — принять живого человека за бота хуже, чем наоборот.
_NAME_PREFIX = re.compile(r"^\s*Антон\s*[,!.]?\s*", re.I)
# Переменные части шаблона: ссылка и название вакансии в кавычках. Без их удаления один
# шаблон рассыпался на десятки «уникальных» текстов: ГигаРекрутер («…на вакансию "Data
# Scientist / MLE" в Telegram https://…») давал 39 ключей на 50 сообщений, рассылка
# опознавалась в 17/50. После чистки — 6 ключей, 50/50, ложных срабатываний 0 (замер 20.07).
_URL = re.compile(r"https?://\S+")
_QUOTED = re.compile(r"[«\"“”][^«»\"“”]{0,80}[»\"“”]")


def norm_text(text: str) -> str:
    """Текст к сравнимому виду: без ссылок, названий вакансий в кавычках и обращения
    по имени — персонализация и подстановки ломают сравнение шаблонов."""
    t = _QUOTED.sub(" ", _URL.sub(" ", text or ""))
    t = " ".join(t.split())
    return _NAME_PREFIX.sub("", t)[:100].lower()


def build_template_index(chats: dict) -> set[str]:
    """Нормализованные тексты работодателей, встречающиеся в корпусе БОЛЬШЕ одного раза."""
    seen: dict[str, int] = {}
    for d in (chats or {}).values():
        for m in d.get("messages") or []:
            if m.get("mine") or not (m.get("text") or "").strip():
                continue
            k = norm_text(m["text"])
            seen[k] = seen.get(k, 0) + 1
    return {k for k, n in seen.items() if n > 1}


# Контакты работодателя в переписке: рекрутёры часто оставляют телефон/телеграм прямо в чате —
# такие вакансии подсвечиваются в ленте бейджем. Email намеренно не ловим: (?<![\w.@]) перед @
# отсекает адреса вида hr@mail.ru, остаются только @ники.
_PHONE_RX = re.compile(r'(?:\+7|\b8)[\s(-]{0,2}\d{3}[\s)-]{0,2}\d{3}[\s-]?\d{2}[\s-]?\d{2}')
_TG_RX = re.compile(r'\bt\.me/[\w+]{4,}|(?<![\w.@])@[A-Za-z]\w{3,}', re.I)


def find_contacts(messages: list[dict]) -> str:
    """Телефон/telegram из сообщений РАБОТОДАТЕЛЯ по ВСЕЙ переписке (контакт ценен и после
    нашего ответа) -> «+7 … · @ник» для бейджа/тултипа; пусто — контактов нет."""
    found: list[str] = []
    for m in messages or []:
        if m.get("mine"):
            continue
        t = m.get("text") or ""
        # В отказах отбрасываем ТОЛЬКО канальные ссылки («другие вакансии: t.me/…»). @ники и
        # телефоны оставляем: reject-классификация бывает ложной — «к сожалению» в проходной
        # клаузе («удалёнки, к сожалению, нет») красит в отказ живой запрос с контактом.
        is_rej = classify(t) is ChatKind.REJECT
        for rx in (_PHONE_RX, _TG_RX):
            for hit in rx.findall(t):
                # t.me/Giga_recruiter_bot и прочие *bot — скрининг-боты (рассылка), не живой
                # контакт рекрутёра; по конвенции Telegram ники ботов кончаются на «bot»
                if hit.lower().endswith("bot"):
                    continue
                if is_rej and hit.lower().startswith("t.me/"):
                    continue
                if hit not in found:
                    found.append(hit)
    return " · ".join(found[:3])


def analyze(messages: list[dict], templates: set[str] | frozenset = frozenset(),
            write_possibility: dict | None = None) -> dict:
    """Свёртка переписки -> состояние для ленты.

    messages: [{"text", "mine", "ts", "bot"}] в порядке отдачи API.
    templates: индекс повторяющихся текстов (см. build_template_index).
    Ждём ответа, только если ПОСЛЕДНЕЕ сообщение — от работодателя (иначе мы уже ответили).
    """
    empty = {"kind": ChatKind.NONE.code, "needs_reply": False, "manual_only": False,
             "sender": "", "preview": "", "ts": "", "contact": ""}
    if not messages:
        return empty
    contact = find_contacts(messages)
    last = messages[-1]
    if last.get("mine"):                       # последнее слово за нами — ждать нечего
        return {**empty, "ts": last.get("ts", ""), "contact": contact}
    text = last.get("text") or ""
    kind = classify(text)
    is_bot = bool(last.get("bot"))
    is_template = norm_text(text) in templates
    # writePossibility из API: часть чатов закрыта на запись (нет приглашения / запретил
    # работодатель) — узнаём заранее, а не по 409 CHAT_DOES_NOT_EXIST при отправке
    wp = (write_possibility or {}).get("name", "")
    return {
        "kind": kind.code,
        "label": kind.label,
        "needs_reply": kind.needs_reply,
        "manual_only": kind is ChatKind.QUESTION and is_manual_only(text),
        "sender": "bot" if is_bot else "template" if is_template else "human",
        "can_write": (not wp) or wp.startswith("ENABLED"),
        "preview": " ".join(text.split())[:160],
        "ts": last.get("ts", ""),
        "contact": contact,
    }
