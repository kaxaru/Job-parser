"""Тесты сборки автоответов. Главное свойство: НЕ пишем туда, куда писать нельзя.

`propose` — чистая функция (без диска и сети), поэтому все гарантии безопасности
проверяются напрямую, без моков транспорта.
"""
import dataclasses

import pytest

from hrwork.application.apply.chat import chat_answer
from hrwork.application.apply.chat.chat_answer import VacancyContext
from hrwork.application.apply.chat.chat_reply import Proposal, propose

PROF_CTX = {"1": VacancyContext(name="Python-разработчик", experience="between1And3")}

# Синтетический профиль для тестов, где проверяется НАБЛЮДАЕМЫЙ ответ, а не факт вызова:
# `propose` зовёт `chat_answer.suggest` без профиля, то есть иначе отвечал бы фактами
# запускающего. Подменять надо у держателя, через которого идёт вызов, — chat_answer.
PROF_FE = {"answers": {
    "stack": ["Python", "FastAPI", "React", "TypeScript"],
    "years_text": "Общий опыт 5 лет 9 мес.",
    "years_frontend_text": "Около 2 лет фронтенда: React, TypeScript.",
}}


def _chat(text, *, bot=False, mine=False, chat_id=555, write="ENABLED"):
    """Один чат в форме chat_messages.json."""
    return {"chatId": chat_id, "write": {"name": write},
            "messages": [{"text": text, "mine": mine, "ts": "2026-07-20T10:00:00", "bot": bot}]}


def _one(chats, ctx=None, answered=frozenset()):
    return propose(chats, ctx if ctx is not None else PROF_CTX, answered)


# ── кому отвечаем ──
def test_answers_bot_question():
    out = _one({"1": _chat("Есть ли у вас опыт работы с Docker?", bot=True)})
    assert [(p.rule, p.sender) for p in out] == [("has_exp_yes", "bot")]


def test_never_answers_human():
    # живому человеку пишет живой человек — даже если движок знает ответ
    assert _one({"1": _chat("Есть ли у вас опыт работы с Docker?", bot=False)}) == []


def test_answers_template_broadcast():
    # одинаковый текст в двух чатах -> рассылка -> отвечаем как боту
    q = "Есть ли у вас опыт работы с Docker?"
    out = _one({"1": _chat(q), "2": _chat(q, chat_id=556)})
    assert [p.sender for p in out] == ["template", "template"]


# ── куда НЕ пишем ──
def test_skips_locked_chat():
    out = _one({"1": _chat("Есть ли опыт с Docker?", bot=True, write="DISABLED")})
    assert out == []


def test_skips_when_last_word_is_ours():
    out = _one({"1": _chat("Есть ли опыт с Docker?", bot=True, mine=True)})
    assert out == []


def test_skips_already_answered_same_question():
    from hrwork.application.apply.chat.chat_reply import _answer_key
    q = "Есть ли опыт с Docker?"
    chats = {"1": _chat(q, bot=True)}
    assert _one(chats, answered=frozenset({_answer_key("1", q)})) == []


def test_answers_new_question_in_same_chat(monkeypatch):
    # бот-интервьюер ведёт цепочку: на первый вопрос ответили, второй — новый.
    # Дедуп по vid запирал бы диалог; дедуп по вопросу пропускает новый.
    #
    # АУДИТ 09.08.2026: тест сверял НАБЛЮДАЕМЫЙ текст ответа, но профиль не подменял —
    # значит читал живой `resume_profile.json` ЗАПУСКАЮЩЕГО (см. шапку файла). У владельца
    # FastAPI в стеке -> зелено; у форка с другим стеком движок ответил бы «нет» или молчал.
    # Плюс вхождение «FastAPI» проходило и на ответе с лишними, выдуманными технологиями.
    from hrwork.application.apply.chat.chat_reply import _answer_key
    monkeypatch.setattr(chat_answer, "load_profile", lambda: PROF_FE)
    answered = frozenset({_answer_key("1", "Есть ли опыт с Docker?")})
    chats = {"1": _chat("А есть ли опыт с FastAPI?", bot=True)}
    out = _one(chats, answered=answered)
    assert [p.rule for p in out] == ["has_exp_yes"]
    assert out[0].text == "Да, есть опыт: FastAPI."


def test_same_question_with_extra_spaces_is_not_answered_twice(monkeypatch):
    # Прежняя форма сравнивала `_answer_key(a) == _answer_key(b)`: обе стороны считала одна
    # и та же приватная функция, и константный ключ («всё уже отвечено») прошёл бы тест
    # (аудит 09.08.2026). Теперь договор проверяется НАБЛЮДАЕМО, с контролем рядом:
    # тот же вопрос с лишними пробелами второй раз не предлагается.
    from hrwork.application.apply.chat.chat_reply import _answer_key
    monkeypatch.setattr(chat_answer, "load_profile", lambda: PROF_FE)
    chats = {"1": _chat("Есть ли у вас  опыт работы  с FastAPI?", bot=True)}
    assert [p.rule for p in _one(chats)] == ["has_exp_yes"]        # контроль: ответ есть
    answered = frozenset({_answer_key("1", "Есть ли у вас опыт работы с FastAPI?")})
    assert _one(chats, answered=answered) == []


@pytest.mark.parametrize("text", [
    "Пройдите первичное интервью с ГигаРекрутером.",          # SCREENING
    "Для связи напишите нам в telegram @recruiter.",           # REDIRECT
    "К сожалению, вынуждены отказать.",                        # REJECT
    "Приглашаем вас на собеседование в четверг.",              # INVITE
])
def test_only_direct_questions(text):
    # скрининг/редирект уводят на внешние формы, отказ и приглашение — не вопросы
    assert _one({"1": _chat(text, bot=True)}) == []


def test_silent_when_engine_has_no_fact():
    out = _one({"1": _chat("Расскажите о самом сложном проекте.", bot=True)})
    assert out == []


# ── деньги и место: предлагаем, но помечаем manual ──
def test_salary_marked_manual_not_auto():
    out = _one({"1": _chat("Укажите ваши зарплатные ожидания.", bot=True)})
    # вилка по грейду вакансии из ctx, и отправлять её должен человек
    assert [(p.rule, p.manual) for p in out] == [("salary_junior", True)]


def test_place_marked_manual():
    out = _one({"1": _chat("Вы готовы работать в г. Шатура?", bot=True)})
    assert [p.manual for p in out] == [True]


def test_salary_silent_without_vacancy_context():
    # вакансия выпала из кеша -> грейд неизвестен -> движок молчит
    assert _one({"1": _chat("Укажите зарплатные ожидания.", bot=True)}, ctx={}) == []


# ── содержимое предложения ──
def test_proposal_carries_full_question_not_preview():
    # в Proposal кладём ПОЛНЫЙ текст: preview обрезан до 160 символов, и вопрос,
    # стоящий в конце длинного письма, движок бы просто не увидел
    long_q = ("Здравствуйте, Антон! Спасибо за ваш отклик и за интерес к нашей вакансии "
              "и к нашей компании. Мы рады вашему резюме. Скажите пожалуйста, есть ли "
              "у вас опыт работы с Docker?")
    assert len(long_q) > 160
    out = _one({"1": _chat(long_q, bot=True)})
    assert [(p.question, p.rule) for p in out] == [(long_q, "has_exp_yes")]


def test_proposal_is_frozen():
    # неизменяемость важна: между сборкой и отправкой текст не должен подмениться
    out = _one({"1": _chat("Есть ли опыт с Docker?", bot=True)})
    with pytest.raises(dataclasses.FrozenInstanceError):
        out[0].text = "подменили"


def test_english_question_answered_in_english():
    prof = {"1": VacancyContext(name="Python Developer", experience="between1And3")}
    out = propose({"1": _chat("Do you have experience with Docker?", bot=True)}, prof)
    assert [p.lang for p in out] == ["en"]


def test_proposal_type():
    out = _one({"1": _chat("Есть ли опыт с Docker?", bot=True)})
    assert isinstance(out[0], Proposal)
    assert out[0].chat_id == 555
    assert out[0].vid == "1"


# ══════════════ poll_replies: точечный опрос после отправки ══════════════
from hrwork.application.apply.chat import chat_reply  # noqa: E402


class _FakeReq:
    """Отдаёт заранее заданную последовательность chat_data по каждому опросу чата."""
    def __init__(self, timeline):
        self.timeline = timeline          # {vid: [data1, data2, ...]} по опросам
        self.calls = dict.fromkeys(timeline, 0)

    def __enter__(self): return self
    def __exit__(self, *_): return False


def _data(*texts_mine):
    """chat_data-подобная структура: [(text, mine), ...] -> items."""
    return {"chat": {"messages": {"items": [
        {"text": t, "canEdit": mine, "type": "SIMPLE", "creationTime": "2026-07-20T00:00"}
        for t, mine in texts_mine]}}}


def _patch(monkeypatch, req):
    monkeypatch.setattr(chat_reply.chat, "find_chat",
                        lambda r, x, vid: (int(vid), 1) if vid in req.timeline else (None, None))
    def fake_chat_data(r, x, cid, aid):
        vid = str(cid)
        i = min(req.calls[vid], len(req.timeline[vid]) - 1)
        req.calls[vid] += 1
        return req.timeline[vid][i]
    monkeypatch.setattr(chat_reply.chat, "chat_data", fake_chat_data)
    saved = {}
    monkeypatch.setattr(chat_reply.store, "chat_messages", lambda: {})
    monkeypatch.setattr(chat_reply.store, "save_chat_messages", lambda d: saved.update(d))
    return saved


def test_poll_stops_chat_once_bot_replies(monkeypatch):
    # первый опрос — последнее слово наше (ждём); второй — ответил бот -> снять с опроса
    req = _FakeReq({"10": [
        _data(("наш ответ", True)),
        _data(("наш ответ", True), ("вопрос бота", False)),
    ]})
    saved = _patch(monkeypatch, req)
    slept = []
    r = chat_reply.poll_replies(req, "x", ["10"], interval_s=60,
                                sleep=slept.append, clock=lambda: len(slept) * 60)
    assert (r["answered"], r["timeout"]) == (1, 0)
    assert saved["10"]["messages"][-1]["mine"] is False       # ответ бота сохранён
    assert req.calls["10"] == 2                                # опрошен ровно 2 раза


def test_poll_times_out_without_reply(monkeypatch):
    req = _FakeReq({"10": [_data(("наш ответ", True))]})       # бот всё молчит
    _patch(monkeypatch, req)
    slept = []
    r = chat_reply.poll_replies(req, "x", ["10"], window_s=180, interval_s=60,
                                sleep=slept.append, clock=lambda: len(slept) * 60)
    assert (r["answered"], r["timeout"]) == (0, 1)
    assert len(slept) == 3                                     # 3 попытки за 180с окно


def test_poll_independent_per_chat(monkeypatch):
    # один чат отвечает сразу, другой молчит — опрос первого прекращается, второй ждёт
    req = _FakeReq({
        "10": [_data(("наш", True), ("ответ бота", False))],
        "20": [_data(("наш", True))],
    })
    _patch(monkeypatch, req)
    slept = []
    r = chat_reply.poll_replies(req, "x", ["10", "20"], window_s=180, interval_s=60,
                                sleep=slept.append, clock=lambda: len(slept) * 60)
    assert (r["answered"], r["timeout"]) == (1, 1)
    assert req.calls["10"] == 1                                # снят после первого же опроса
    assert req.calls["20"] == 3                                # опрашивался всё окно


def test_poll_skips_unfound_chat(monkeypatch):
    req = _FakeReq({})
    _patch(monkeypatch, req)
    r = chat_reply.poll_replies(req, "x", ["999"], sleep=lambda s: None, clock=lambda: 0)
    assert r == {"answered": 0, "timeout": 0, "updated": 0}


# ══════════════ --include-manual: деньги/место только по согласию ══════════════
from hrwork.application.apply.chat.chat_reply import select_targets  # noqa: E402


def _prop(vid, manual):
    return Proposal(vid=vid, chat_id=int(vid), question="q?", text="a", rule="r",
                    lang="ru", sender="bot", manual=manual)


def test_manual_excluded_by_default():
    auto = [_prop("1", False)]
    man = [_prop("2", True)]
    out = select_targets(auto, man, include_manual=False, consent=lambda p: True)
    assert [p.vid for p in out] == ["1"]           # manual не попал, даже если бы согласились


def test_manual_included_only_with_consent():
    auto = [_prop("1", False)]
    man = [_prop("2", True), _prop("3", True)]
    # согласились только на 3
    out = select_targets(auto, man, include_manual=True, consent=lambda p: p.vid == "3")
    assert [p.vid for p in out] == ["1", "3"]


def test_manual_all_declined():
    out = select_targets([_prop("1", False)], [_prop("2", True)],
                         include_manual=True, consent=lambda p: False)
    assert [p.vid for p in out] == ["1"]


def test_console_consent_refuses_without_tty(monkeypatch):
    # ключевая защита: без интерактивного терминала вилка НЕ уходит
    import sys

    from hrwork.application.apply.chat.chat_reply import _console_consent
    class _NoTty:
        def isatty(self): return False
    monkeypatch.setattr(sys, "stdin", _NoTty())
    assert _console_consent(_prop("2", True)) is False


def test_console_consent_yes_on_tty(monkeypatch):
    import builtins
    import sys

    from hrwork.application.apply.chat import chat_reply
    class _Tty:
        def isatty(self): return True
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(builtins, "input", lambda *_: "y")
    assert chat_reply._console_consent(_prop("2", True)) is True
    monkeypatch.setattr(builtins, "input", lambda *_: "")
    assert chat_reply._console_consent(_prop("2", True)) is False


# ══════════════ --loop: диалог до конца (цикл раундов) ══════════════
def test_loop_continues_across_different_chats(monkeypatch):
    from hrwork.application.apply.chat import chat_reply
    # loop продолжается по РАЗНЫМ чатам: р1 отвечает чату 10, р2 — новому чату 20, р3 пусто.
    plans = iter([([_prop("10", False)], []),   # раунд 1: чат 10
                  ([_prop("20", False)], []),   # раунд 2: другой чат 20 (первый уже готов)
                  ([], [])])                    # раунд 3: нечего -> стоп
    monkeypatch.setattr(chat_reply, "_plan", lambda only, limit, classify=None: next(plans))
    monkeypatch.setattr(chat_reply, "_send_batch", lambda req, x, t: [p.vid for p in t])
    monkeypatch.setattr(chat_reply, "poll_replies",
                        lambda req, x, vids, **k: {"answered": len(vids), "timeout": 0, "updated": 1})
    class _Req:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr("hrwork.application.apply.session.open_client", lambda: (_Req(), "x"))
    r = chat_reply.run(send=True, max_rounds=8)
    assert (r["sent"], r["rounds"]) == (2, 3)


def test_loop_antiloop_same_chat_answered_once_per_run(monkeypatch):
    # ANTI-LOOP: бот переспрашивает (план всё время даёт тот же чат 10) -> отвечаем
    # ОДИН раз за прогон, дальше пропуск. БАГ 21.07: 5 отправок про «стаж с React».
    from hrwork.application.apply.chat import chat_reply
    monkeypatch.setattr(chat_reply, "_plan", lambda only, limit, classify=None: ([_prop("10", False)], []))
    sent_calls = []
    monkeypatch.setattr(chat_reply, "_send_batch",
                        lambda req, x, t: (sent_calls.append([p.vid for p in t]) or [p.vid for p in t]))
    monkeypatch.setattr(chat_reply, "poll_replies",
                        lambda req, x, vids, **k: {"answered": len(vids), "timeout": 0, "updated": 1})
    class _Req:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr("hrwork.application.apply.session.open_client", lambda: (_Req(), "x"))
    r = chat_reply.run(send=True, max_rounds=8)
    assert r["sent"] == 1                          # чат 10 отвечен РОВНО раз, не 8
    assert sent_calls == [["10"]]                  # _send_batch вызван единожды


def test_loop_stops_when_bot_silent(monkeypatch):
    from hrwork.application.apply.chat import chat_reply
    monkeypatch.setattr(chat_reply, "_plan", lambda only, limit, classify=None: ([_prop("10", False)], []))
    monkeypatch.setattr(chat_reply, "_send_batch", lambda req, x, t: [p.vid for p in t])
    # бот НЕ ответил (answered=0) -> цикл прерывается после первого раунда
    monkeypatch.setattr(chat_reply, "poll_replies",
                        lambda req, x, vids, **k: {"answered": 0, "timeout": len(vids), "updated": 1})
    class _Req:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr("hrwork.application.apply.session.open_client", lambda: (_Req(), "x"))
    r = chat_reply.run(send=True, max_rounds=8)
    assert (r["sent"], r["rounds"]) == (1, 1)      # молчание бота -> не крутим дальше


def test_single_round_by_default(monkeypatch):
    from hrwork.application.apply.chat import chat_reply
    plans = iter([([_prop("10", False)], []), ([_prop("10", False)], [])])
    monkeypatch.setattr(chat_reply, "_plan", lambda only, limit, classify=None: next(plans))
    monkeypatch.setattr(chat_reply, "_send_batch", lambda req, x, t: [p.vid for p in t])
    monkeypatch.setattr(chat_reply, "poll_replies",
                        lambda req, x, vids, **k: {"answered": len(vids), "timeout": 0, "updated": 1})
    class _Req:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    monkeypatch.setattr("hrwork.application.apply.session.open_client", lambda: (_Req(), "x"))
    r = chat_reply.run(send=True)                  # max_rounds=1 по умолчанию
    assert r["rounds"] == 1                         # один раунд, петли нет


def test_confirm_is_manual_not_auto():
    # «Используем эти ответы?» -> confirm -> НЕ отправляется авто: бот подтверждает
    # утверждения, которые проставил сам, а не наши (найдено на живом чате 21.07)
    out = _one({"1": _chat("Используем эти ответы?", bot=True)})
    assert [(p.rule, p.manual) for p in out] == [("confirm", True)]


# ══════════════ classify inject в propose ══════════════
def test_propose_uses_injected_classifier(monkeypatch):
    from hrwork.application.apply.chat.chat_intent import IntentResult
    calls = []
    def classify(q):
        calls.append(q)
        return IntentResult("years_tech", ("React",))
    # Метка долетает внутрь — проверяется НАБЛЮДАЕМЫМ ответом, а не только фактом вызова
    # мока: без доехавшего intent вопрос «сколько лет с этим фреймворком» ушёл бы в общий
    # `years`, а не во фронтенд-зонтик (аудит 09.08.2026).
    monkeypatch.setattr(chat_answer, "load_profile", lambda: PROF_FE)
    ctx = {"1": VacancyContext(name="React dev", experience="between1And3")}
    chats = {"1": _chat("Сколько лет с этим фреймворком?", bot=True)}
    out = propose(chats, ctx, classify=classify)
    assert calls == ["Сколько лет с этим фреймворком?"]     # классификатор вызван
    assert [(p.rule, p.text) for p in out] == [
        ("frontend", "Около 2 лет фронтенда: React, TypeScript.")]


def test_propose_default_no_classifier_no_call():
    # дефолт classify=None -> intent не считается, работает regex-путь (сеть не нужна)
    ctx = {"1": VacancyContext(name="Python dev", experience="between1And3")}
    chats = {"1": _chat("Есть ли опыт с Docker?", bot=True)}
    out = propose(chats, ctx)                                 # без classify
    assert [p.rule for p in out] == ["has_exp_yes"]


# ══════════════ etap-2: LLM-переформулировка (human-gated) ══════════════
def _prop_r(vid, rule, text="ИСТОЧНИК"):
    return Proposal(vid=vid, chat_id=int(vid), question="Есть опыт с Docker?", text=text,
                    rule=rule, lang="ru", sender="bot", manual=False)


def test_make_rephraser_none_when_disabled(monkeypatch):
    from hrwork.application.apply.chat import chat_rephrase, chat_reply
    monkeypatch.setattr(chat_rephrase, "REPHRASE_ENABLED", False)
    assert chat_reply._make_rephraser() is None           # выключено -> переформулировщика нет


def test_apply_rephrase_ineligible_verbatim_no_call():
    from hrwork.application.apply.chat.chat_reply import _apply_rephrase
    called = []
    def rephrase(q, s, r, lang):
        called.append(1)
        return "НОВОЕ"
    out = _apply_rephrase([_prop_r("1", "salary_junior")], rephrase, consent=lambda p, c: True)
    assert out[0].text == "ИСТОЧНИК"                      # не eligible -> дословно
    assert called == []                                   # ... и сеть не зовётся


def test_apply_rephrase_unchanged_is_auto_no_consent():
    from hrwork.application.apply.chat.chat_reply import _apply_rephrase
    asked = []
    out = _apply_rephrase([_prop_r("1", "has_exp_yes")],
                          rephrase=lambda q, s, r, lang: "ИСТОЧНИК",          # без изменений
                          consent=lambda p, c: asked.append(1) or True)
    assert out[0].text == "ИСТОЧНИК"
    assert asked == []                                    # не изменилось -> consent не спрашивают


def test_apply_rephrase_changed_consented_replaces():
    from hrwork.application.apply.chat.chat_reply import _apply_rephrase
    out = _apply_rephrase([_prop_r("1", "has_exp_yes")],
                          rephrase=lambda q, s, r, lang: "ПЕРЕФОРМУЛИРОВАНО",
                          consent=lambda p, c: True)
    assert out[0].text == "ПЕРЕФОРМУЛИРОВАНО"


def test_apply_rephrase_changed_declined_keeps_source():
    from hrwork.application.apply.chat.chat_reply import _apply_rephrase
    out = _apply_rephrase([_prop_r("1", "has_exp_yes")],
                          rephrase=lambda q, s, r, lang: "ПЕРЕФОРМУЛИРОВАНО",
                          consent=lambda p, c: False)                     # крон/отказ
    assert out[0].text == "ИСТОЧНИК"


def _run_with_rephrase(monkeypatch, *, isatty, input_val):
    """run(send, use_rephrase=True) с всегда-меняющей переформулировкой; вернёт список
    ТЕКСТОВ, реально ушедших в _send_batch."""
    import builtins
    import sys

    from hrwork.application.apply.chat import chat_rephrase, chat_reply
    monkeypatch.setattr(chat_rephrase, "REPHRASE_ENABLED", True)
    monkeypatch.setattr(chat_rephrase, "rephrase_answer", lambda q, s, r, lang: "ПЕРЕФОРМУЛИРОВАНО")
    monkeypatch.setattr(chat_reply, "_plan",
                        lambda only, limit, classify=None: ([_prop_r("10", "has_exp_yes")], []))

    class _Std:
        def isatty(self):
            return isatty
    monkeypatch.setattr(sys, "stdin", _Std())
    monkeypatch.setattr(builtins, "input", lambda *_: input_val)
    captured = []
    monkeypatch.setattr(chat_reply, "_send_batch",
                        lambda req, x, t: (captured.extend(pp.text for pp in t) or [pp.vid for pp in t]))
    monkeypatch.setattr(chat_reply, "poll_replies",
                        lambda req, x, vids, **k: {"answered": 0, "timeout": len(vids), "updated": 0})

    class _Req:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
    monkeypatch.setattr("hrwork.application.apply.session.open_client", lambda: (_Req(), "x"))
    chat_reply.run(send=True, use_rephrase=True)
    return captured


def test_run_rephrase_no_tty_sends_source(monkeypatch):
    # СТРАЖ-РЕГРЕСС M-3: под кроном (нет tty) изменённая переформулировка НЕ уходит -> источник
    assert _run_with_rephrase(monkeypatch, isatty=False, input_val="y") == ["ИСТОЧНИК"]


def test_run_rephrase_tty_yes_sends_candidate(monkeypatch):
    # интерактивно с подтверждением -> уходит переформулировка
    assert _run_with_rephrase(monkeypatch, isatty=True, input_val="y") == ["ПЕРЕФОРМУЛИРОВАНО"]


def test_run_rephrase_tty_no_sends_source(monkeypatch):
    # интерактивно, но человек отказал -> источник дословно
    assert _run_with_rephrase(monkeypatch, isatty=True, input_val="n") == ["ИСТОЧНИК"]
