"""Пути состояния по аккаунтам (RFC-004, R1/R2/R6).

Пути — константы, которые печёт импорт `config.py` по `HR_ACCOUNT`, поэтому каждый аккаунт
проверяется в ОТДЕЛЬНОМ процессе. Второй аккаунт подменяет `resolve_account` до импорта
config (`hrwork/accounts.py`): заводить `data/accounts/acc2/` в реальных данных тест не имеет права.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_PROBE = r"""
import json, sys
import hrwork.accounts as accounts
from hrwork.domain.account import HhAccount, accounts_dir
if sys.argv[1] != "main":
    accounts.resolve_account = lambda raw, data_dir: HhAccount("acc2", "B", accounts_dir(data_dir) / "acc2")
from hrwork import config
from hrwork.application.apply import account_session, browser, session
from hrwork.application.apply.chat import chat_reply
from hrwork.application.apply.runtime import bump_state, lock, quota
from hrwork.infrastructure.storage import followup, marks
paths = {
    "session.STATE_FILE": session.STATE_FILE,
    "browser.PROFILE_DIR": browser.PROFILE_DIR,
    "quota.QUOTA_FILE": quota.QUOTA_FILE,
    "bump_state.BUMP_FILE": bump_state.BUMP_FILE,
    "followup.CHAT_MESSAGES_FILE": followup.CHAT_MESSAGES_FILE,
    "followup.FORM_VACANCIES_FILE": followup.FORM_VACANCIES_FILE,
    "followup.RESPONSE_STATUS_FILE": followup.RESPONSE_STATUS_FILE,
    "followup.APPLIED_LOG_FILE": followup.APPLIED_LOG_FILE,
    "followup.PENDING_FILE": followup.PENDING_FILE,
    "chat_reply.REPLIES_LOG": chat_reply.REPLIES_LOG,
    "config._RESUME_FILE": config._RESUME_FILE,
    "account_session.IDENTITY_FILE": account_session.IDENTITY_FILE,
    "account_session.SESSION_STATUS_FILE": account_session.SESSION_STATUS_FILE,
    "followup.FORM_CACHE_FILE": followup.FORM_CACHE_FILE,
    "marks.MARKS_FILE": marks.MARKS_FILE,
    "lock.LOCK_FILE": lock.LOCK_FILE,
    "config.RAW_FILE": config.RAW_FILE,
}
out = {k: v.relative_to(config.BASE_DIR).as_posix() for k, v in paths.items()}
out["FORMS_ENABLED"] = config.FORMS_ENABLED
print("PROBE" + json.dumps(out))
"""


def _probe(account: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "HR_ACCOUNT"}
    env["FORMS_LLM"] = "1"
    run = subprocess.run([sys.executable, "-c", _PROBE, account], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("PROBE")), None)
    assert line is not None, run.stderr[-2000:]
    return json.loads(line[len("PROBE"):])


@pytest.fixture(scope="module")
def main_paths() -> dict:
    return _probe("main")


@pytest.fixture(scope="module")
def acc2_paths() -> dict:
    return _probe("acc2")


# R1: основной аккаунт — ровно прежние пути, миграции нет.
@pytest.mark.parametrize("name, expected", [
    ("session.STATE_FILE", "data/hh_state.json"),
    ("browser.PROFILE_DIR", "data/browser_profile"),
    ("quota.QUOTA_FILE", "data/apply_quota.json"),
    ("bump_state.BUMP_FILE", "data/bump_state.json"),
    ("followup.CHAT_MESSAGES_FILE", "data/chat_messages.json"),
    ("followup.FORM_VACANCIES_FILE", "data/form_vacancies.json"),
    ("followup.RESPONSE_STATUS_FILE", "data/response_status.json"),
    ("followup.APPLIED_LOG_FILE", "data/applied_log.jsonl"),
    ("followup.PENDING_FILE", "data/apply_pending.json"),
    ("chat_reply.REPLIES_LOG", "data/chat_replies.jsonl"),
    ("config._RESUME_FILE", "resume_profile.json"),
    ("account_session.IDENTITY_FILE", "data/account_identity.json"),
    ("account_session.SESSION_STATUS_FILE", "data/session_status.json"),
    ("followup.FORM_CACHE_FILE", "data/forms_cache.json"),
    ("marks.MARKS_FILE", "data/marks.json"),
    ("lock.LOCK_FILE", "data/autoclick.lock"),
    ("config.RAW_FILE", "data/vacancies_raw.json"),
])
def test_main_account_keeps_legacy_paths(main_paths, name, expected):
    assert main_paths[name] == expected


# R2: второй аккаунт — своё состояние в своей папке; кеш, отметки, lock и снятые анкеты общие.
@pytest.mark.parametrize("name, expected", [
    ("session.STATE_FILE", "data/accounts/acc2/hh_state.json"),
    ("browser.PROFILE_DIR", "data/accounts/acc2/browser_profile"),
    ("quota.QUOTA_FILE", "data/accounts/acc2/apply_quota.json"),
    ("bump_state.BUMP_FILE", "data/accounts/acc2/bump_state.json"),
    ("followup.CHAT_MESSAGES_FILE", "data/accounts/acc2/chat_messages.json"),
    ("followup.FORM_VACANCIES_FILE", "data/accounts/acc2/form_vacancies.json"),
    ("followup.RESPONSE_STATUS_FILE", "data/accounts/acc2/response_status.json"),
    ("followup.APPLIED_LOG_FILE", "data/accounts/acc2/applied_log.jsonl"),
    ("followup.PENDING_FILE", "data/accounts/acc2/apply_pending.json"),
    ("chat_reply.REPLIES_LOG", "data/accounts/acc2/chat_replies.jsonl"),
    ("config._RESUME_FILE", "data/accounts/acc2/resume_profile.json"),
    ("account_session.IDENTITY_FILE", "data/accounts/acc2/account_identity.json"),
    ("account_session.SESSION_STATUS_FILE", "data/accounts/acc2/session_status.json"),
    ("followup.FORM_CACHE_FILE", "data/forms_cache.json"),
    ("marks.MARKS_FILE", "data/marks.json"),
    ("lock.LOCK_FILE", "data/autoclick.lock"),
    ("config.RAW_FILE", "data/vacancies_raw.json"),
])
def test_second_account_state_lives_in_its_folder(acc2_paths, name, expected):
    assert acc2_paths[name] == expected


# R6: анкеты заполняются профилем и резюме основного — второму они закрыты даже при FORMS_LLM=1.
@pytest.mark.parametrize("paths_fixture, expected", [("main_paths", True), ("acc2_paths", False)])
def test_forms_are_enabled_only_for_the_main_account(request, paths_fixture, expected):
    assert request.getfixturevalue(paths_fixture)["FORMS_ENABLED"] is expected
