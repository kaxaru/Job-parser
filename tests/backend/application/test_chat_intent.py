"""Классификатор намерения. Главные свойства: битый/чужой ответ -> None (fallback),
выключено/нет ключа -> сеть не зовётся. Транспорт замокан, реальных вызовов нет."""
import os

import pytest

from hrwork.application.apply.chat import chat_intent
from hrwork.application.apply.chat.chat_intent import IntentResult, _parse_intent


# ── _parse_intent: чистая функция парсинга/валидации ──
def test_parse_clean_json():
    r = _parse_intent('{"intent":"years_tech","tech":["React"]}')
    assert r == IntentResult("years_tech", ("React",))


def test_parse_strips_json_fences():
    r = _parse_intent('```json\n{"intent":"has_exp","tech":["Docker"]}\n```')
    assert r == IntentResult("has_exp", ("Docker",))


def test_parse_extracts_object_from_noise():
    r = _parse_intent('Вот ответ: {"intent":"salary","tech":[]} — готово')
    assert r.label == "salary"
    assert r.tech == ()


@pytest.mark.parametrize("raw", [
    None, "", "не json вовсе",
    '{"intent":"выдуманная_метка"}',        # метка вне набора
    '{"tech":["React"]}',                    # нет intent
    '{"intent": 123}',                        # не строка
])
def test_parse_bad_returns_none(raw):
    assert _parse_intent(raw) is None


def test_parse_tech_only_valid_strings():
    r = _parse_intent('{"intent":"has_exp","tech":["React","",null,"  ",42]}')
    assert r.tech == ("React", "42")          # пустые/пробельные отброшены, число -> "42"


# ── classify_intent: оркестрация + fallback ──
def _enable(monkeypatch, transport):
    monkeypatch.setattr(chat_intent, "INTENT_ENABLED", True)
    monkeypatch.setattr(chat_intent, "chat_json", transport)


def test_disabled_no_network(monkeypatch):
    called = []
    monkeypatch.setattr(chat_intent, "INTENT_ENABLED", False)
    monkeypatch.setattr(chat_intent, "chat_json", lambda *a, **k: called.append(1))
    assert chat_intent.classify_intent("Сколько лет с React?") is None
    assert called == []                       # выключено -> сеть НЕ дёрнута


def test_enabled_classifies(monkeypatch):
    _enable(monkeypatch, lambda s, u, **k: '{"intent":"years_tech","tech":["React"]}')
    r = chat_intent.classify_intent("Сколько лет вы работаете с React?")
    assert r == IntentResult("years_tech", ("React",))


def test_transport_none_returns_none(monkeypatch):
    _enable(monkeypatch, lambda *a, **k: None)   # сеть/ключ упали
    assert chat_intent.classify_intent("Сколько лет с React?") is None


def test_empty_question_no_call(monkeypatch):
    called = []
    _enable(monkeypatch, lambda *a, **k: called.append(1))
    assert chat_intent.classify_intent("   ") is None
    assert called == []


def test_question_truncated_and_no_profile_leak(monkeypatch):
    seen = {}
    def transport(system, user, **k):
        seen["system"] = system
        seen["user"] = user
        return '{"intent":"other","tech":[]}'
    _enable(monkeypatch, transport)
    chat_intent.classify_intent("React " * 500)
    # АУДИТ 09.08.2026: длина сверялась с ПРИВАТНОЙ константой реализации и через `<=`
    # (усечение до 5 символов тоже прошло бы), а «утечки профиля нет» проверялось
    # отсутствием слова «Тольятти» — города КОНКРЕТНОГО владельца, то есть на любом другом
    # профиле утверждение бессмысленно. Теперь длина — литерал спеки промпта (500 символов),
    # а отсутствие утечки — ПОЛОЖИТЕЛЬНОЕ утверждение: в user РОВНО усечённый вопрос.
    assert len(seen["user"]) == 500
    assert seen["user"] == ("React " * 84)[:500]
    # system — константа без подстановок, поэтому фактам профиля туда попасть неоткуда
    assert seen["system"] is chat_intent._SYSTEM


# ══════════════ live: реальный OpenRouter (opt-in) ══════════════
# Медленный, сетевой, требует OPEN_ROUTER_API_KEY — вне обычного CI. Как test_prescreen.
@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("INTENT_LIVE"),
                    reason="сетевой вызов OpenRouter — opt-in: INTENT_LIVE=1 pytest")
@pytest.mark.parametrize("question,expected", [
    ("Сколько лет вы работаете с React в коммерческих проектах?", "years_tech"),
    ("Есть ли опыт с Docker?", "has_exp"),
    ("Сколько лет опыта в разработке?", "years"),
    ("Укажите зарплатные ожидания", "salary"),
])
def test_live_classification(monkeypatch, question, expected):
    monkeypatch.setattr(chat_intent, "INTENT_ENABLED", True)
    r = chat_intent.classify_intent(question)
    assert r is not None, "OpenRouter не ответил (ключ/сеть?)"
    assert r.label == expected, f"{question!r} -> {r.label} (ждали {expected})"
