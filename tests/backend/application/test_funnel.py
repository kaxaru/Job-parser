"""Воронка автоотказов — латентность отказа и агрегация по компаниям (без сети/браузера).

Защищаемые свойства: корректная раскладка латентности по бакетам, отделение отказов с
измеримой латентностью (был чат-отказ) от статусных DISCARD без сообщения, приглашение не
считается отказом, неизвестный работодатель -> «вне выдачи», перекос меток отбрасывается,
своё сообщение никогда не читается как отказ работодателя.
"""
import datetime as dt
import types

import pytest

from hrwork.application import funnel

_APPLY = "2026-07-01T10:00:00"                       # наивная метка -> трактуется как МСК
_APPLY_DT = dt.datetime(2026, 7, 1, 10, 0, 0)


def _reject_msg(when: dt.datetime, *, mine: bool = False, bot: bool = False) -> dict:
    return {"text": "к сожалению, вынуждены отказать", "mine": mine,
            "ts": when.isoformat() + "+03:00", "bot": bot}


def _repo(id_to_employer: dict) -> list:
    return [types.SimpleNamespace(id=i, vacancy=types.SimpleNamespace(employer=e))
            for i, e in id_to_employer.items()]


class _Store:
    def __init__(self, applied: dict, statuses: dict, chats: dict):
        self._a, self._s, self._c = applied, statuses, chats

    def applied_log(self):
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
    _wire(monkeypatch, {"v1": _APPLY}, {}, {"v1": {"messages": [_reject_msg(rej)]}},
          _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 1 and o["measured"] == 1
    assert (o["le_10m"], o["le_1h"], o["le_1d"], o["slow_reject"]) == (le10, le1h, le1d, slow)


def test_discard_status_without_chat_is_rejected_but_unmeasured(monkeypatch):
    # отказ по статусу, сообщения в чате нет -> латентность неизвестна (сегмент «без метки»)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"}, {}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 1 and o["measured"] == 0 and o["le_1d"] == 0


def test_invited_status_counts_as_invited_not_reject(monkeypatch):
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "INVITATION"}, {}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["invited"] == 1 and o["rejected"] == 0 and o["no_response"] == 0


def test_unknown_employer_bucketed_as_out_of_feed(monkeypatch):
    # вакансия ушла из выдачи -> работодатель не сохранён -> «(вне выдачи)»
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"}, {}, _repo({}))
    comps = {c["employer"]: c for c in funnel.compute_funnel()["companies"]}
    assert comps["(вне выдачи)"]["rejected"] == 1


def test_negative_latency_dropped_from_measured(monkeypatch):
    # отказ РАНЬШЕ отклика (перекос часов) — считаем отказом, но латентность недостоверна
    rej = _APPLY_DT - dt.timedelta(minutes=30)
    _wire(monkeypatch, {"v1": _APPLY}, {}, {"v1": {"messages": [_reject_msg(rej)]}},
          _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 1 and o["measured"] == 0


def test_own_message_never_counts_as_employer_reject(monkeypatch):
    # «к сожалению» в НАШЕМ сообщении не должно краситься в отказ работодателя
    msg = _reject_msg(_APPLY_DT + dt.timedelta(minutes=5), mine=True)
    _wire(monkeypatch, {"v1": _APPLY}, {}, {"v1": {"messages": [msg]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["rejected"] == 0 and o["no_response"] == 1


def test_company_auto_reject_rate_is_le_1h_share(monkeypatch):
    # у ACME два отказа: один за 5 мин, один за 2 суток -> автобан-доля = 50%
    fast = _APPLY_DT + dt.timedelta(minutes=5)
    slow = _APPLY_DT + dt.timedelta(minutes=3000)
    chats = {"a": {"messages": [_reject_msg(fast)]}, "b": {"messages": [_reject_msg(slow)]}}
    _wire(monkeypatch, {"a": _APPLY, "b": _APPLY}, {}, chats, _repo({"a": "ACME", "b": "ACME"}))
    c = {x["employer"]: x for x in funnel.compute_funnel()["companies"]}["ACME"]
    assert c["rejected"] == 2 and c["le_1h"] == 1 and c["auto_reject_rate"] == 50.0
