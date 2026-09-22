"""Владение пулом двух аккаунтов (RFC-004: R24, R25).

acc2 — приоритет: берёт весь свой пул (`reject` -> None). Основной ОТСТУПАЕТ: вычитает весь
`eligible.json` acc2 целиком и делает это НЕЗАВИСИМО от того, жива ли сессия acc2. При
устаревшем eligible основной всё равно вычитает последний известный набор — недобрать
безопаснее, чем пустить дубль. Прежний A/B-50/50 по хешу отменён (решение владельца 16.09.2026).
"""
import json

import pytest

from hrwork.application.apply import ab_split, taken
from hrwork.domain.account import HhAccount

CODES = ("main", "acc2")
ACC2 = HhAccount(code="acc2", label="B", data_dir=None)
STAMP = 1_789_000_000.0
FOREIGN = "A/B: доля другого аккаунта"


# ── R24: чья вакансия идёт в пул ──
@pytest.mark.parametrize("me, vid, expected", [
    ("main", "3", None),          # не в пуле acc2 -> основной берёт
    ("main", "1", FOREIGN),       # в пуле acc2 -> основной отступает
    ("main", "2", FOREIGN),
    ("acc2", "1", None),          # acc2 берёт весь свой пул
    ("acc2", "3", None),          # даже не в eligible — acc2 не режется ничем
])
def test_main_subtracts_whole_acc2_pool_and_acc2_takes_everything(me, vid, expected):
    split = ab_split.AbSplit(codes=CODES, me=me, eligible=frozenset({"1", "2"}), eligible_fresh=True)
    assert split.reject(vid) == expected


def test_main_defers_on_the_whole_pool_even_when_stale():
    split = ab_split.AbSplit(codes=CODES, me="main", eligible=frozenset({"1"}), eligible_fresh=False)
    assert (split.reject("1"), split.reject("9")) == (FOREIGN, None)


@pytest.mark.parametrize("me, vid, expected", [
    ("main", "1", True), ("main", "9", False),
    ("acc2", "9", True),          # для acc2 весь его отклик — из его пула (ab=True)
])
def test_journal_ab_flag(me, vid, expected):
    split = ab_split.AbSplit(codes=CODES, me=me, eligible=frozenset({"1", "2"}), eligible_fresh=True)
    assert split.in_common(vid) is expected


# ── Файл пула (eligible.json) ──
@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(taken, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(ab_split, "collected_at", lambda: STAMP)
    return tmp_path


def _account(root, code):
    folder = root / "accounts" / code
    folder.mkdir(parents=True)
    (folder / "account.json").write_text("{}", encoding="utf-8")
    return folder


def _eligible(folder, ids, stamp=STAMP):
    (folder / "eligible.json").write_text(json.dumps(
        {"built_at": "x", "cache_collected_at": stamp, "ids": ids}), encoding="utf-8")


def test_single_account_has_no_split(root):
    assert ab_split.current_split() is None


def test_second_account_writes_its_pool_with_the_cache_stamp(root, monkeypatch):
    folder = _account(root, "acc2")
    monkeypatch.setattr(ab_split, "ACCOUNT", ACC2)
    ab_split.write_eligible(["3", "1", "3"])
    data = json.loads((folder / "eligible.json").read_text(encoding="utf-8"))
    assert (data["ids"], data["cache_collected_at"]) == (["1", "3"], STAMP)


def test_main_never_writes_a_pool(root):
    _account(root, "acc2")
    with pytest.raises(ab_split.AbSplitError) as err:
        ab_split.write_eligible(["1"])
    assert str(err.value) == "пул acc2 пишет только второй аккаунт — у основного свои правила"


def test_fresh_pool_builds_the_split_for_main(root):
    folder = _account(root, "acc2")
    _eligible(folder, ["1", "2"])
    assert ab_split.current_split() == ab_split.AbSplit(
        codes=CODES, me="main", eligible=frozenset({"1", "2"}), eligible_fresh=True)


# Решение владельца: acc2 без ни одного прогона (нет eligible.json) в делёж ещё не вошёл —
# сплит выключен, основной берёт весь пул (двойной отклик ловит taken.py).
def test_missing_pool_disables_the_split(root):
    _account(root, "acc2")
    assert ab_split.current_split() is None


def test_stale_pool_still_builds_the_split(root):
    folder = _account(root, "acc2")
    _eligible(folder, ["1"], stamp=STAMP - 3600)
    assert ab_split.current_split() == ab_split.AbSplit(
        codes=CODES, me="main", eligible=frozenset({"1"}), eligible_fresh=False)


# ── R25: основной отступает от пула acc2 НЕЗАВИСИМО от сессии acc2 (в т.ч. когда acc2 в дауне) ──
def test_main_defers_regardless_of_acc2_session(root):
    folder = _account(root, "acc2")          # session_status.json acc2 отсутствует (сессия «мертва»)
    _eligible(folder, ["1", "2"])
    split = ab_split.current_split()
    assert split is not None and split.me == "main" and split.reject("1") == FOREIGN


def test_acc2_split_reads_its_own_pool(root, monkeypatch):
    folder = _account(root, "acc2")
    _eligible(folder, ["1"])
    monkeypatch.setattr(ab_split, "ACCOUNT", ACC2)
    split = ab_split.current_split()
    assert split is not None and split.me == "acc2" and split.reject("1") is None


def test_three_accounts_are_refused(root):
    _account(root, "acc2")
    _account(root, "acc3")
    with pytest.raises(ab_split.AbSplitError) as err:
        ab_split.current_split()
    assert str(err.value) == "A/B-сплит рассчитан на два аккаунта, заведены: main, acc2, acc3"


# ── Хелпер account_session.session_alive удалён (аудит 22.09.2026, §4): после S8 деление пула
#    на живость сессии не завязано, прод-вызовов не было, жил только этими тестами ──
