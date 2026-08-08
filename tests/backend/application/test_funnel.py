"""Воронка автоотказов — латентность отказа и агрегация по компаниям (без сети/браузера).

Защищаемые свойства: исход ТОЛЬКО по статусу HH (этажи взаимоисключающие), reject-фраза
без статуса-отказа не считается отказом (ложные срабатывания classify — отдельный счётчик),
корректная раскладка латентности по бакетам, дубли журнала схлопываются в ранний МОМЕНТ,
работодатель берётся из выдачи либо из журнала, перекос меток отбрасывается.
"""
import csv
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
    assert (o["rejected"], o["measured"]) == (1, 1)
    assert (o["le_10m"], o["le_1h"], o["le_1d"], o["slow_reject"]) == (le10, le1h, le1d, slow)


def test_discard_status_without_chat_is_rejected_but_unmeasured(monkeypatch):
    # отказ по статусу, сообщения в чате нет -> латентность неизвестна (сегмент «без метки»)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"}, {}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert (o["rejected"], o["measured"], o["le_1d"]) == (1, 0, 0)


def test_chat_reject_without_status_is_not_reject(monkeypatch):
    # reject-фраза в чате БЕЗ статуса-отказа: у classify известные ложные срабатывания
    # («удалёнки, к сожалению, нет») — не отказ, но виден счётчиком chat_reject_only
    rej = _APPLY_DT + dt.timedelta(minutes=5)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "RESPONSE"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert (o["rejected"], o["no_outcome"], o["chat_reject_only"]) == (0, 1, 1)


def test_floors_are_exclusive_interview_with_reject_phrase(monkeypatch):
    # статус INTERVIEW + reject-фраза в переписке -> ТОЛЬКО invited (не двойной счёт)
    rej = _APPLY_DT + dt.timedelta(minutes=5)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "INTERVIEW"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    # Все четыре этажа литералами одним кортежем. Прежний второй ассерт сверял выход
    # воронки САМ С СОБОЙ (applied == invited + rejected + no_outcome): комбинация
    # applied=2 / no_outcome=1 — то есть двойной учёт отклика, ради которого тест
    # и написан, — уравнению удовлетворяет и проходила (аудит 09.08.2026).
    assert (o["applied"], o["invited"], o["rejected"], o["no_outcome"]) == (1, 1, 0, 0)


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
    assert (o["applied"], o["le_1h"]) == (1, 1)      # латентность 30 мин от РАННЕГО ts


def test_negative_latency_dropped_from_measured(monkeypatch):
    # отказ РАНЬШЕ отклика (перекос часов) — считаем отказом, но латентность недостоверна
    rej = _APPLY_DT - dt.timedelta(minutes=30)
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert (o["rejected"], o["measured"]) == (1, 0)


def test_own_message_never_counts_as_employer_reject(monkeypatch):
    # «к сожалению» в НАШЕМ сообщении не красит чат в отказ и не попадает в chat_reject_only
    msg = _reject_msg(_APPLY_DT + dt.timedelta(minutes=5), mine=True)
    _wire(monkeypatch, {"v1": _APPLY}, {}, {"v1": {"messages": [msg]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert (o["rejected"], o["no_outcome"], o["chat_reject_only"]) == (0, 1, 0)


def test_company_auto_reject_rate_from_measured(monkeypatch):
    # у ACME два ИЗМЕРЕННЫХ отказа: 5 мин и 2 суток -> автобан-доля = 1/2 = 50%
    fast = _APPLY_DT + dt.timedelta(minutes=5)
    slow = _APPLY_DT + dt.timedelta(minutes=3000)
    chats = {"a": {"messages": [_reject_msg(fast)]}, "b": {"messages": [_reject_msg(slow)]}}
    _wire(monkeypatch, {"a": _APPLY, "b": _APPLY},
          {"a": "DISCARD", "b": "DISCARD"}, chats, _repo({"a": "ACME", "b": "ACME"}))
    c = {x["employer"]: x for x in funnel.compute_funnel()["companies"]}["ACME"]
    assert (c["rejected"], c["measured"]) == (2, 2)
    assert (c["le_1h"], c["auto_reject_rate"]) == (1, 50.0)


def test_reject_latency_measured_from_last_reject_message(monkeypatch):
    # БАГ 08.08.2026: латентность бралась от ПЕРВОГО reject-сообщения. Живое «удалёнки,
    # к сожалению, нет» через 5 минут после отклика — известный false positive classify —
    # уводило компанию в бакет мгновенного автобана, хотя настоящий отказ пришёл через
    # двое суток. Берём ПОСЛЕДНИЙ отказ: ошибка в сторону «человек», а не «ATS-бан».
    soft = {"text": "удалёнки, к сожалению, нет", "mine": False,
            "ts": (_APPLY_DT + dt.timedelta(minutes=5)).isoformat(), "bot": False}
    real = _reject_msg(_APPLY_DT + dt.timedelta(minutes=2880))
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"},
          {"v1": {"messages": [soft, real]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["measured"] == 1
    assert (o["le_10m"], o["le_1h"], o["le_1d"], o["slow_reject"]) == (0, 0, 0, 1)
    assert o["median_reject_min"] == 2880


def test_reject_latency_independent_of_message_order_in_cache(monkeypatch):
    # порядок сообщений в кеше переписки не гарантирован контрактом — берём МАКСИМАЛЬНУЮ
    # метку среди отказов, а не «последний элемент списка»
    soft = {"text": "удалёнки, к сожалению, нет", "mine": False,
            "ts": (_APPLY_DT + dt.timedelta(minutes=5)).isoformat(), "bot": False}
    real = _reject_msg(_APPLY_DT + dt.timedelta(minutes=2880))
    _wire(monkeypatch, {"v1": _APPLY}, {"v1": "DISCARD"},
          {"v1": {"messages": [real, soft]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["median_reject_min"] == 2880


def test_journal_duplicates_collapse_by_moment_not_iso_string(monkeypatch):
    # БАГ 08.08.2026: дубли схлопывались сравнением ISO-СТРОК, а в журнале сосуществуют
    # +04:00 (append_applied) и +03:00 (метки HH). «09:30+03:00» (06:30Z) строкой меньше
    # «10:00+04:00» (06:00Z), хотя реально ранний ВТОРОЙ. Отказ в 07:05Z: от настоящего
    # раннего момента это 65 мин (бакет <=1д), от строкового — 35 мин (бакет <=1ч).
    log = [{"id": "v1", "ts": "2026-07-01T09:30:00+03:00"},
           {"id": "v1", "ts": "2026-07-01T10:00:00+04:00"}]
    rej = dt.datetime(2026, 7, 1, 7, 5, tzinfo=dt.timezone.utc)
    _wire(monkeypatch, log, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert o["measured"] == 1
    assert (o["le_10m"], o["le_1h"], o["le_1d"]) == (0, 0, 1)
    assert o["median_reject_min"] == 65


@pytest.mark.parametrize("broken", ["", "0000-99-99T00:00:00", "не дата", "2026-13-45T99:00"])
def test_broken_journal_timestamp_loses_to_valid_one(monkeypatch, broken):
    # метка приходит с диска: битая/пустая не роняет воронку и НЕ побеждает валидную
    # (строковое сравнение отдавало победу «0000-99-99…» — отклик терял измеримую латентность)
    log = [{"id": "v1", "ts": broken}, {"id": "v1", "ts": _APPLY}]
    rej = _APPLY_DT + dt.timedelta(minutes=30)
    _wire(monkeypatch, log, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert (o["applied"], o["measured"]) == (1, 1)
    assert o["median_reject_min"] == 30


@pytest.mark.parametrize("broken", ["", "0000-99-99T00:00:00", "не дата"])
def test_all_timestamps_broken_keeps_response_without_latency(monkeypatch, broken):
    # если ВСЕ метки id битые — отклик остаётся в воронке отказом, латентность неизмерима
    log = [{"id": "v1", "ts": broken}, {"id": "v1", "ts": broken}]
    rej = _APPLY_DT + dt.timedelta(minutes=30)
    _wire(monkeypatch, log, {"v1": "DISCARD"},
          {"v1": {"messages": [_reject_msg(rej)]}}, _repo({"v1": "ACME"}))
    o = funnel.compute_funnel()["overall"]
    assert (o["applied"], o["rejected"], o["measured"]) == (1, 1, 0)


def test_csv_column_for_invited_states_is_named_positive_not_invitation(monkeypatch, tmp_path):
    # 08.08.2026: колонка называлась «Приглашений», а считает INVITED_STATES, куда входит
    # CONSIDER («Рассматривается») — это ещё не приглашение. Набор един с лентой НАМЕРЕННО
    # (fix.md №6), поэтому правится подпись, а не набор.
    _wire(monkeypatch, {"v1": _APPLY, "v2": _APPLY}, {"v1": "DISCARD", "v2": "CONSIDER"},
          {}, _repo({"v1": "ACME", "v2": "ACME"}))
    out = tmp_path / "13_company_funnel.csv"
    funnel.write_funnel_csv(out)
    with open(out, encoding="utf-8-sig", newline="") as f:
        header, *rows = list(csv.reader(f))
    assert header == ["Компания", "Откликов", "Отказов", "Измерено", "<=10м", "<=1ч",
                      "<=1д", "Позитив/в работе", "Медиана мин", "Автобан % (от измеренных)"]
    assert rows[0][0] == "ACME"
    assert rows[0][7] == "1"                         # CONSIDER считается «в работе»


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
