"""Тесты классификации переписки: тип сообщения + КТО написал (бот/шаблон/человек)."""
import pytest

from hrwork.application.apply.chat.chat_class import (
    FROZEN_CODES,
    FROZEN_KINDS,
    ChatKind,
    analyze,
    build_template_index,
    classify,
    find_contacts,
    is_manual_only,
    norm_text,
)


def _m(text, mine=False, bot=False):
    return {"text": text, "mine": mine, "ts": "2026-07-19T10:00:00+03:00", "bot": bot}


# ── тип сообщения ──
def test_classify_order_screening_before_question():
    # у бот-скрининга тоже бывает «?», но отвечать в чат бессмысленно — уводит на внешнюю форму
    assert classify("Пройдите первичное интервью, это ускорит рассмотрение?") is ChatKind.SCREENING
    assert classify("К сожалению, мы не готовы пригласить вас") is ChatKind.REJECT
    assert classify("Буду рад обсудить детали, когда вам удобно?") is ChatKind.INVITE
    assert classify("Занимались ли вы A/B-тестами?") is ChatKind.QUESTION
    assert classify("Мы получили ваш отклик") is ChatKind.OTHER


def test_imperative_ask_without_question_mark():
    # боты спрашивают повелительным наклонением, «?» может не быть вовсе
    assert classify("Укажите, пожалуйста, ваши зарплатные ожидания") is ChatKind.QUESTION
    assert classify("Расскажите про опыт с Django") is ChatKind.QUESTION
    # но «заполните анкету» — это внешний скрининг, а не вопрос в чате
    assert classify("Спасибо за отклик! Заполните небольшую анкету") is ChatKind.SCREENING
    assert classify("Приглашаем к заполнению анкеты") is ChatKind.SCREENING


# ── бот-интервью в чужом мессенджере: полумёртвая ветка, фриз ──
# Текст снят с живого письма, но токены deep-link ЗАМЕНЕНЫ на синтетические (08.08.2026):
# настоящий `?start=…` — одноразовый инвайт, привязанный к кандидатуре, и в публичной
# фикстуре он приглашал бы постороннего в чужую интервью-сессию. Детекту важна только
# форма ссылки (`?start=`), а не значение токена.
_GIGA = ("Здравствуйте! Пройдите короткое первичное интервью с ГигаРекрутером на вакансию "
         "\"Инженер по нагруженному тестированию\". Это позволит быстрее рассмотреть вашу "
         "кандидатуру. Вы можете пройти интервью в Максе: "
         "https://max.ru/giga_recruiter_bot?start=Mx7kQp2v в Telegram: "
         "https://t.me/Giga_recruiter_bot?start=Tg9wZr4nLs6b")


@pytest.mark.parametrize("txt", [
    _GIGA,
    # у Сбера несколько юрлиц-прокладок с идентичными письмами: имя компании в детекте не
    # участвует, ловим бот-ссылку с ?start= (deep-link в бота, а не живой @handle)
    "Добрый день! Для продолжения пройдите интервью: https://t.me/hr_screening_bot?start=Ab12Cd",
    "Наш AI рекрутер задаст вам пару вопросов, пройдите по ссылке",
    "Приглашаем на интервью с ботом",
])
def test_bot_interview_is_frozen_not_screening(txt):
    kind = classify(txt)
    assert kind is ChatKind.BOT_INTERVIEW
    assert kind in FROZEN_KINDS          # полумёртвое: даже пройденное у бота часто морозится
    assert kind.needs_reply is True      # бейдж в ленте рисуется только для ждущих


@pytest.mark.parametrize("txt", [
    "Меня зовут Иван, напишите мне в телеграм @ivan_hr, обсудим вакансию",   # живой рекрутёр
    "Свяжитесь с нами в whatsapp по номеру из профиля",
])
def test_human_channel_redirect_is_not_bot_interview(txt):
    assert classify(txt) is ChatKind.REDIRECT


def test_frozen_kinds_is_single_source_for_feed():
    # лента берёт коды через инжект CHAT_FROZEN_PY; расхождение = 'ack' снова захардкожен в JS
    assert FROZEN_CODES == ("ack", "bot_interview")
    assert {k.code for k in FROZEN_KINDS} == {"ack", "bot_interview"}


def test_ack_boilerplate_is_frozen():
    # «резюме получено, изучим, свяжемся, благодарим за интерес» — тупик/фриз, висит неделями,
    # не вопрос. Помечаем «🧊 фриз», чтобы не путать с живыми вопросами в ленте.
    txt = ("Уважаемый соискатель, Ваше резюме получено. Мы внимательно его изучим и "
           "свяжемся с вами. Благодарим за проявленный интерес к нашей компании.")
    assert classify(txt) is ChatKind.ACK
    assert analyze([_m(txt)])["kind"] == "ack"
    assert ChatKind.ACK.label == "🧊 фриз"


def test_ack_does_not_swallow_real_question():
    # заглушка + реальный вопрос -> QUESTION (вопрос проверяется ДО фриза)
    assert classify("Резюме получено. Сколько лет опыта с Python?") is ChatKind.QUESTION
    # «получили ваш отклик» (без «резюме») — по-прежнему OTHER, детектор узкий
    assert classify("Мы получили ваш отклик") is ChatKind.OTHER


def test_salary_ask_is_manual_even_without_question_mark():
    # первый вопрос бота часто про деньги — и без «?»; отвечать должен человек
    a = analyze([_m("Укажите, пожалуйста, ваши зарплатные ожидания в формате: ХХХХХ - ХХХХХ")])
    assert a["kind"] == "question" and a["manual_only"] is True


def test_reject_never_needs_reply():
    # HH закрывает чат на запись после отказа (409 CHAT_DOES_NOT_EXIST) — подсвечивать нечего
    assert ChatKind.REJECT.needs_reply is False
    assert ChatKind.QUESTION.needs_reply is True


# ── ждём ответа только если работодатель написал ПОСЛЕДНИМ ──
def test_no_reply_expected_when_we_wrote_last():
    msgs = [_m("Есть опыт с Python?"), _m("Да, есть", mine=True)]
    assert analyze(msgs)["needs_reply"] is False


def test_reply_expected_when_they_wrote_last():
    a = analyze([_m("Здравствуйте!", mine=True), _m("Есть опыт с Python?")])
    assert a["needs_reply"] is True and a["kind"] == "question"


# ── КТО написал: два независимых признака ──
def test_sender_bot_by_api_flag():
    a = analyze([_m("С каким ML-фреймворком работали?", bot=True)])
    assert a["sender"] == "bot"


def test_sender_template_by_corpus_repeat():
    # шаблон от ИМЕНИ ЖИВОГО рекрутера: isBot=False, ловится только повтором текста
    tmpl = "Рассмотрим ваше резюме. Если подойдёт, свяжемся."
    chats = {"1": {"messages": [_m(f"Антон, здравствуйте! {tmpl}")]},
             "2": {"messages": [_m(f"Антон, здравствуйте! {tmpl}")]}}
    idx = build_template_index(chats)
    a = analyze(chats["1"]["messages"], idx)
    assert a["sender"] == "template"


def test_sender_human_when_unique_and_not_bot():
    chats = {"1": {"messages": [_m("Антон, расскажите про ваш проект на Django")]}}
    a = analyze(chats["1"]["messages"], build_template_index(chats))
    assert a["sender"] == "human"


def test_norm_strips_personalization():
    # персонализация по имени не должна мешать находить одинаковые шаблоны
    assert norm_text("Антон, здравствуйте! Спасибо") == norm_text("Здравствуйте! Спасибо")


def test_our_own_messages_not_counted_as_templates():
    chats = {"1": {"messages": [_m("Здравствуйте!", mine=True)]},
             "2": {"messages": [_m("Здравствуйте!", mine=True)]}}
    assert build_template_index(chats) == set()


# ── writePossibility: часть чатов закрыта на запись ──
def test_can_write_from_api():
    q = [_m("Есть опыт?")]
    assert analyze(q, write_possibility={"name": "ENABLED_FOR_ALL"})["can_write"] is True
    assert analyze(q, write_possibility={"name": "DISABLED_FOR_APPLICANT"})["can_write"] is False
    assert analyze(q)["can_write"] is True          # поля нет (старые данные) -> не мешаем


# ── деньги/переезд: бот отвечать не должен ──
def test_manual_only_for_salary_and_relocation():
    assert analyze([_m("Рассматриваете вилку до 100 на руки?")])["manual_only"] is True
    assert analyze([_m("Готовы на переезд в Казань?")])["manual_only"] is True
    assert analyze([_m("Есть опыт с Django?")])["manual_only"] is False


# ══════════════ REDIRECT: перевод в другой канал (20.07) ══════════════
# 11 из 19 «молчащих вопросов» оказались редиректами в telegram/по ссылке — им нельзя
# отвечать в чат by design, и они засоряли QUESTION повелительным наклонением.
@pytest.mark.parametrize("text", [
    "У вас интересное резюме. Для связи напишите нам в telegram @ninaroket.",
    "Чтобы принять участие в отборе, переходите в наш Telegram-бот по ссылке ниже.",
    "если вы ознакомились с условиями, напишите в телеграмм директору @ilkhaaan",
])
def test_redirect_detected(text):
    assert classify(text) is ChatKind.REDIRECT
    assert ChatKind.REDIRECT.needs_reply        # внимание нужно: пойти по ссылке


def test_reject_with_telegram_link_stays_reject():
    # отказ терминален независимо от ссылок: «к сожалению … другие вакансии t.me/…»
    assert classify(
        "К сожалению, мы не готовы рассматривать вашу кандидатуру. "
        "Другие вакансии: https://t.me/it_job_offers"
    ) is ChatKind.REJECT


def test_email_is_not_redirect():
    # «hr@mail.ru» — почта, а не telegram-handle (@… с lookbehind)
    assert classify("Пришлите резюме на hr@mail.ru") is not ChatKind.REDIRECT


# ══════════════ norm_text v2: подстановки не дробят шаблон (20.07) ══════════════
def test_norm_text_strips_vacancy_name_and_url():
    # ГигаРекрутер: один шаблон давал 39 ключей на 50 сообщений (название вакансии
    # в кавычках + уникальная ссылка) -> рассылка опознавалась в 17/50. После чистки 50/50.
    a = ('Здравствуйте! Пройдите короткое первичное интервью с ГигаРекрутером на вакансию '
         '"Data Scientist / MLE" в Telegram https://t.me/Giga_bot?start=abc123')
    b = ('Здравствуйте! Пройдите короткое первичное интервью с ГигаРекрутером на вакансию '
         '"Middle Python Developer" в Telegram https://t.me/Giga_bot?start=xyz789')
    assert norm_text(a) == norm_text(b)


def test_norm_text_distinct_texts_stay_distinct():
    assert norm_text("Спасибо за отклик, мы вернёмся с ответом.") \
        != norm_text("К сожалению, вынуждены отказать.")


# ══════════════ manual_only: дыры 20.07 закрыты единым источником ══════════════
@pytest.mark.parametrize("text", [
    "Какой у вас ожидаемый уровень заработной платы на этой должности?",
    "Здравствуйте, Вы готовы работать в г. Шатура ?",
])
def test_manual_only_holes_closed(text):
    # «заработной платы» не ловилось «зарплат», «готовы работать в г. X» — «переезд».
    # Теперь SALARY_Q/PLACE_Q — единый источник с chat_answer, дыры чинятся один раз.
    assert is_manual_only(text)


# ── контакты работодателя в переписке (телефон/telegram -> бейдж в ленте) ──
def test_contact_phone_and_tg_from_employer():
    msgs = [_m("Здравствуйте! Наш HR: @hr_nick, тел. +7 912 345-67-89")]
    out = analyze(msgs)
    assert "@hr_nick" in out["contact"] and "+7 912 345-67-89" in out["contact"]


def test_contact_survives_our_last_word():
    # контакт ценен, даже если последнее слово за нами (needs_reply=False)
    msgs = [_m("пишите в t.me/hr_team"), _m("хорошо, напишу", mine=True)]
    out = analyze(msgs)
    assert "t.me/hr_team" in out["contact"] and out["needs_reply"] is False


@pytest.mark.parametrize("text", [
    "пишите на hr@mail.ru",          # email — не телеграм-ник ((?<![\w.@]) отсекает)
    "ждём вас в офисе завтра",       # контактов нет
])
def test_contact_not_detected(text):
    assert find_contacts([_m(text)]) == ""


def test_contact_ignores_our_own_messages():
    assert find_contacts([_m("мой ник @my_own_nick", mine=True)]) == ""


def test_contact_skips_telegram_bots():
    # t.me/Giga_recruiter_bot — скрининг-рассылка, не личный контакт; ники ботов кончаются на bot
    msgs = [_m("Пройдите интервью: t.me/Giga_recruiter_bot или пишите @hr_lena")]
    assert find_contacts(msgs) == "@hr_lena"


def test_contact_channel_link_in_reject_is_noise():
    # «к сожалению … другие вакансии: t.me/…» — канал из отказа, а не контакт для связи
    msgs = [_m("К сожалению, мы не готовы предложить вам работу. Другие вакансии: t.me/it_job_offers")]
    assert find_contacts(msgs) == ""


def test_contact_nick_survives_false_reject():
    # живой кейс 134891594: запрос «направьте данные в телеграмм @vicky_2909» красится в REJECT
    # из-за проходного «к сожалению, не предусмотрено» — @ник терять нельзя
    msgs = [_m("Просим направить информацию в телеграмм @vicky_2909. "
               "Полностью удаленной работы, к сожалению, не предусмотрено.")]
    assert find_contacts(msgs) == "@vicky_2909"
