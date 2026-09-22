"""Какие режимы CLI доступны не-основному аккаунту (RFC-004, R7).

Второму аккаунту открыт только `autoclick`. Сбор и лента пишут ОБЩИЙ кеш и сверяют его с
запросами и городами профиля — от второго профиля кеш пересобрался бы под него; сервер
отдаёт журналы всех аккаунтов; чаты и анкеты второму не нужны и отвечали бы фактами из
резюме основного.
"""
import argparse

import pytest

import hh
from hrwork.domain.account import HhAccount

MAIN = HhAccount(code="main", label="основной", data_dir=None)
ACC2 = HhAccount(code="acc2", label="B", data_dir=None)


@pytest.mark.parametrize("mode", list(hh.Mode))
def test_main_account_may_run_every_mode(mode):
    assert hh.account_mode_error(mode, MAIN) is None


def test_second_account_may_run_autoclick():
    assert hh.account_mode_error(hh.Mode.AUTOCLICK, ACC2) is None


@pytest.mark.parametrize("mode, expected", [
    (hh.Mode.ALL, "Аккаунт acc2: режим all только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.COLLECT, "Аккаунт acc2: режим collect только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.ENRICH, "Аккаунт acc2: режим enrich только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.ANALYZE, "Аккаунт acc2: режим analyze только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.DASHBOARD, "Аккаунт acc2: режим dashboard только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.FEED, "Аккаунт acc2: режим feed только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.SERVE, "Аккаунт acc2: режим serve только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.CHAT, "Аккаунт acc2: режим chat только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.FORMS, "Аккаунт acc2: режим forms только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
    (hh.Mode.HHAPI, "Аккаунт acc2: режим hhapi только для основного аккаунта (RFC-004) — сними HR_ACCOUNT"),
])
def test_second_account_is_refused_shared_and_main_only_modes(mode, expected):
    assert hh.account_mode_error(mode, ACC2) == expected


def test_dry_pool_flag_reports_the_pool_without_a_browser(monkeypatch):
    from hrwork.application.apply import dry_pool
    called: list[str] = []
    monkeypatch.setattr(dry_pool, "run", lambda write_eligible: called.append(("dry-pool", write_eligible)))
    hh._do_autoclick(argparse.Namespace(dry_pool=True, write_eligible=True))   # без login/sync/apply
    assert called == [("dry-pool", True)]


def test_refused_mode_stops_before_its_handler(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(hh, "ACCOUNT", ACC2)
    monkeypatch.setitem(hh._HANDLERS, hh.Mode.COLLECT, lambda args: called.append("collect"))
    with pytest.raises(SystemExit) as stop:
        hh.Mode.COLLECT.run(argparse.Namespace())
    assert str(stop.value) == ("Аккаунт acc2: режим collect только для основного аккаунта (RFC-004) — "
                               "сними HR_ACCOUNT")
    assert called == []
