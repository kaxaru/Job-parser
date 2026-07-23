"""Воронка автоотказов — латентность отказа и агрегация по компаниям (без сети/браузера).

Защищаемые свойства: исход ТОЛЬКО по статусу HH (этажи взаимоисключающие), reject-фраза
без статуса-отказа не считается отказом (ложные срабатывания classify — отдельный счётчик),
корректная раскладка латентности по бакетам, дубли журнала схлопываются в ранний ts,
работодатель берётся из выдачи либо из журнала, перекос меток отбрасывается.
"""
import datetime as dt
import types

import pytest

from hrwork.application import funnel

_APPLY = "2026-07-01T10:00:00+04:00"                 # aware-метка журнала (локальная зона)
_APPLY_DT = dt.datetime(2026, 7, 1, 10, 0, 0,
                        tzinfo=dt.timezone(dt.timedelta(hours=4)))


def _reject_msg(when: dt.datetime, *, mine: bool = False, bot: bool = False) -> dict:
    return {"text": "к сожалению, вынуждены отказать", "mine": mine,
            "ts": when.isoformat(), "bot": bot}


def _repo(id_to_employer: dict) -> list:
    return [types.SimpleNamespace(id=i, vacancy=types.SimpleNamespace(employer=e))
            for i, e in id_to_employer.items()]


class _Store:
    def __init__(self, applied, statuses, chats):
        self._a, self._s, self._c = applied, statuses, chats

    def applied_log(self):
        if isinstance(self._a, list):                # список записей как есть (тест дублей)
            return self._a
        return [{"id": k, "ts": v} for k, v in self._a.items()]

    def statuses(self):
        return self._s

    def chat_messages(self):
        return self._c


def _wire(mp, applied, statuses, chats, repo):
    mp.setattr(funnel, "store", _Store(applied, statuses, chats))
    mp.setattr(funnel, "vacancy_repository",
               lambda: types.SimpleNamespace(load=lambda: repo))


@pytest.mark.parametrize("offset_min, le10, le1h, le1d, slow", [
    (5,    1, 1, 1, 0),     # мгновенный автобан
    (30,   0, 1, 1, 0),
    (300,  0, 0, 1, 0),     # в тот же день
    (3000, 0, 0, 0, 1),     # >1 дня — скорее человек
])
def test_reject_latency_buckets(monkeypatch, offset_min, le10, le1h, le1d, slow):
    rej = _APPLY_DT + dt.timedelta(minutes=offset_min)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 1 and o["measured"] == 1
    assert (o["le_10m"], o["le_1h"], o["le_1d"], o["slow_reject"]) == (le10, le1h, le1d, slow)


def test_discard_status_without_chat_is_rejected_but_unmeasured(monkeypatch):
    # отказ по статусу, сообщения в чате нет -> латентность неизвестна (сегмент «без метки»)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"}, {}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 1 and o["measured"] == 0 and o["le_1d"] == 0


def test_chat_reject_without_status_is_not_reject(monkeypatch):
    # reject-фраза в чате БЕЗ статуса-отказа: у classify известные ложные срабатывания
    # («удалёнки, к сожалению, нет») — не отказ, но виден счётчиком chat_reject_only
    rej = _APPLY_DT + dt.timedelta(minutes=5)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "RESPONSE"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 0 and o["no_outcome"] == 1 and o["chat_reject_only"] == 1


def test_floors_are_exclusive_interview_with_reject_phrase(monkeypatch):
    # статус INTERVIEW + reject-фраза в переписке -> ТОЛЬКО invited (не двойной счёт)
    rej = _APPLY_DT + dt.timedelta(minutes=5)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "INTERVIEW"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["invited"] == 1 and o["rejected"] == 0
    assert o["applied"] == o["invited"] + o["rejected"] + o["no_outcome"]


def test_unknown_employer_falls_back_to_journal_then_out_of_feed(monkeypatch):
    # вакансия ушла из выдачи: работодатель из журнала (пишется при отклике);
    # старая запись без employer -> «(вне выдачи)»
    log = [{"id": "v1", "ts": _APPLY, "employer": "ACME"},
           {"id": "v2", "ts": _APPLY}]
    _wire(monkeypatch, log, {"v1": "DISCARD", "v2": "DISCARD"}, {}, _repo({}))
    comps = {c["employer"]: c for c in funnel.compute_funnel()["companies"]}
    assert comps["ACME"]["rejected"] == 1
    assert comps["(вне выдачи)"]["rejected"] == 1


def test_journal_duplicates_collapse_to_earliest_ts(monkeypatch):
    # дубль id в append-only журнале -> берём ранний ts (реальный момент отклика)
    late = "2026-07-02T10:00:00+04:00"
    log = [{"id": "v1", "ts": late}, {"id": "v1", "ts": _APPLY}]
    rej = _APPLY_DT + dt.timedelta(minutes=30)
    _wire(monkeypatch, log, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["applied"] == 1 and o["le_1h"] == 1     # латентность 30 мин от РАННЕГО ts


def test_negative_latency_dropped_from_measured(monkeypatch):
    # отказ РАНЬШЕ отклика (перекос часов) — считаем отказом, но латентность недостоверна
    rej = _APPLY_DT - dt.timedelta(minutes=30)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 1 and o["measured"] == 0


def test_own_message_never_counts_as_employer_reject(monkeypatch):
    # «к сожалению» в НАШЕМ сообщении не красит чат в отказ и не попадает в chat_reject_only
    msg = _reject_msg(_APPLY_DT + dt.timedelta(minutes=5), mine=True)
    _wire(monkeypatch, {"v1": _APPLY}, {}, {"v1": {"messages": [msg]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 0 and o["no_outcome"] == 1 and o["chat_reject_only"] == 0


def test_company_auto_reject_rate_from_measured(monkeypatch):
    # у ACME два ИЗМЕРЕННЫХ отказа: 5 мин и 2 суток -> автобан-доля = 1/2 = 50%
    fast = _APPLY_DT + dt.timedelta(minutes=5)
    slow = _APPLY_DT + dt.timedelta(minutes=3000)
    chats = {"a": {"messages": [_reject_msg(fast)]}, "b": {"messages": [_reject_msg(slow)]}}
    _wire(monkeypatch, {"a": _APPLY, "b": _APPLY},
          {"a": "DISCARD", "b": "DISCARD"}, chats, _repo({"a": "ACME", "b": "ACME"}))
    c = {x["employer"]: x for x in funnel.compute_funnel()["companies"]}["ACME"]
    assert c["rejected"] == 2 and c["measured"] == 2
    assert c["le_1h"] == 1 and c["auto_reject_rate"] == 50.0


def test_small_sample_companies_rank_below_min_n(monkeypatch):
    # 1/1=100% НЕ должен обгонять компанию с достаточной выборкой (>=3 измеренных)
    fast = _APPLY_DT + dt.timedelta(minutes=5)
    applied = dict.fromkeys(("a1", "a2", "a3", "b1"), _APPLY)
    statuses = dict.fromkeys(applied, "DISCARD")
    chats = {v: {"messages": [_reject_msg(fast)]} for v in applied}
    repo = _repo({"a1": "BigCo", "a2": "BigCo", "a3": "BigCo", "b1": "TinyCo"})
    _wire(monkeypatch, applied, statuses, chats, repo)
    comps = funnel.compute_funnel()["companies"]
    assert comps[0]["employer"] == "BigCo"           # 3/3 измерено -> ранжируется первой
