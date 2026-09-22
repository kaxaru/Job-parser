"""Второй слой «одна вакансия — один аккаунт» (RFC-004, R12).

A/B-сплит разводит пулы аккаунтов по построению, но не видит ручного клика в ленте, отклика
руками на hh.ru и очереди ожидания. Поэтому журналы и очереди ДРУГИХ аккаунтов проверяются
при отборе, перед кликом и на путях мимо отбора.
"""
import json

import pytest

from hrwork.application.apply import autoclick, taken
from hrwork.application.apply.ab_split import PoolInputs
from hrwork.application.apply.candidates import pick_candidates
from hrwork.application.apply.outcome import ApplyOutcome
from hrwork.domain.account import HhAccount
from hrwork.infrastructure.storage import JsonVacancyRepository

pytestmark = pytest.mark.usefixtures("apply_defaults")

ACC2 = HhAccount(code="acc2", label="B", data_dir=None)


def _journal(path, *ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"id": i, "name": "n"}) + "\n" for i in ids), encoding="utf-8")


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """main: журнал 1, 2; acc2: журнал 3, очередь 4; «acc9» без account.json — не аккаунт."""
    monkeypatch.setattr(taken, "DATA_ROOT", tmp_path)
    _journal(tmp_path / "applied_log.jsonl", "1", "2")
    (tmp_path / "apply_pending.json").write_text("[]", encoding="utf-8")
    acc2 = tmp_path / "accounts" / "acc2"
    _journal(acc2 / "applied_log.jsonl", "3")
    (acc2 / "apply_pending.json").write_text(json.dumps([{"id": "4", "url": "u"}]), encoding="utf-8")
    (acc2 / "account.json").write_text("{}", encoding="utf-8")
    _journal(tmp_path / "accounts" / "acc9" / "applied_log.jsonl", "9")
    return tmp_path


def test_main_sees_what_the_second_account_took(accounts):
    assert taken.taken_by_others() == {"3": "acc2", "4": "acc2"}


def test_second_account_sees_what_main_took(accounts, monkeypatch):
    monkeypatch.setattr(taken, "ACCOUNT", ACC2)
    assert taken.taken_by_others() == {"1": "main", "2": "main"}


def test_single_account_takes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(taken, "DATA_ROOT", tmp_path)
    _journal(tmp_path / "applied_log.jsonl", "1")
    assert taken.taken_by_others() == {}


# ── R26: own_handled_ids — дедуп приоритетного аккаунта против СВОЕЙ истории ──
def _own_files(tmp_path, monkeypatch, applied=(), pending=(), statuses=None):
    from hrwork.infrastructure.storage import followup
    _journal(tmp_path / "applied_log.jsonl", *applied)
    (tmp_path / "apply_pending.json").write_text(
        json.dumps([{"id": i, "url": "u"} for i in pending]), encoding="utf-8")
    (tmp_path / "response_status.json").write_text(json.dumps(statuses or {}), encoding="utf-8")
    monkeypatch.setattr(followup, "APPLIED_LOG_FILE", tmp_path / "applied_log.jsonl")
    monkeypatch.setattr(followup, "PENDING_FILE", tmp_path / "apply_pending.json")
    monkeypatch.setattr(followup, "RESPONSE_STATUS_FILE", tmp_path / "response_status.json")


def test_own_handled_ids_unions_journal_pending_and_statuses(tmp_path, monkeypatch):
    # "1" есть и в журнале, и в статусах -> побеждает журнал (setdefault, первый источник)
    _own_files(tmp_path, monkeypatch, applied=("1", "2"), pending=("3",),
               statuses={"4": "Отказ", "1": "Отклик"})
    assert taken.own_handled_ids() == {"1": "applied", "2": "applied", "3": "pending", "4": "status"}


def test_own_handled_ids_does_not_read_other_accounts(tmp_path, monkeypatch):
    _own_files(tmp_path, monkeypatch, applied=("1",))
    _journal(tmp_path / "accounts" / "acc2" / "applied_log.jsonl", "9")   # чужой журнал
    assert taken.own_handled_ids() == {"1": "applied"}


def _hh(vid):
    return JsonVacancyRepository._from_dict({
        "id": vid, "name": "Python Backend разработчик", "area": {"id": "1", "name": "Москва"},
        "salary": None, "experience": {"id": "between1And3"}, "schedule": {"id": "remote"},
        "snippet": {"requirement": "FastAPI, Docker", "responsibility": ""},
        "alternate_url": f"https://hh.ru/vacancy/{vid}", "employer": {"name": "Acme"},
        "created_at": None, "_source": "hh",
    })


def test_vacancy_taken_by_another_account_never_enters_the_pool():
    stats: dict[str, int] = {}
    pool = pick_candidates([_hh("1"), _hh("2")], PoolInputs(taken={"2"}), limit=10, stats=stats)
    assert [c.id for c in pool] == ["1"]
    assert stats == {"занята другим аккаунтом": 1}


def test_feed_apply_refuses_a_vacancy_taken_by_another_account(accounts, monkeypatch):
    clicks: list[str] = []
    monkeypatch.setattr(autoclick, "apply_one",
                        lambda page, cand, **k: clicks.append(cand.id) or ApplyOutcome.APPLIED)
    monkeypatch.setattr(autoclick, "_VACANCY_META", {})
    result = autoclick._apply_one_vacancy(None, "4", "https://hh.ru/vacancy/4", "", name="n")
    assert result == {"status": "taken", "owner": "acc2", "letter": False}
    assert clicks == []
