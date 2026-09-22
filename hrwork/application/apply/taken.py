"""Вакансии, занятые ДРУГИМИ аккаунтами (RFC-004): второй слой «одна вакансия — один аккаунт».

Первый слой — A/B-сплит: пулы аккаунтов не пересекаются по построению. Он не видит того, что
сплит обходит: ручной клик в ленте, отклик руками прямо на hh.ru (его дожурналит синк),
очередь ожидания ленты. Поэтому перед отбором, перед КАЖДЫМ кликом и в `/api/apply`
сверяемся с журналами и очередями остальных аккаунтов. Отметки `marks.json` общие и
проверяются отбором и так.

С одним аккаунтом других нет — слой ничего не меняет.
"""
from pathlib import Path

from hrwork.config import ACCOUNT, DATA_DIR
from hrwork.domain.account import ACCOUNT_META_FILE, MAIN_CODE, accounts_dir
from hrwork.infrastructure.storage import followup

DATA_ROOT = DATA_DIR        # откуда искать папки аккаунтов (тесты подменяют)


def account_dirs() -> dict[str, Path]:
    """{код: папка состояния} всех заведённых аккаунтов, включая текущий."""
    dirs = {MAIN_CODE: DATA_ROOT}
    root = accounts_dir(DATA_ROOT)
    if root.is_dir():
        dirs.update({d.name: d for d in sorted(root.iterdir())
                     if (d / ACCOUNT_META_FILE).is_file()})
    return dirs


def taken_by_others() -> dict[str, str]:
    """{id вакансии: код аккаунта} — отклики из журналов и очереди ожидания ДРУГИХ аккаунтов.
    Читается с диска на каждый вызов: занятость меняется, пока идёт прогон."""
    out: dict[str, str] = {}
    for code, folder in account_dirs().items():
        if code == ACCOUNT.code:
            continue
        for rec in followup.load_applied_log(folder / followup.APPLIED_LOG_FILE.name):
            out.setdefault(str(rec.get("id")), code)
        for rec in followup.load_pending(folder / followup.PENDING_FILE.name):
            out.setdefault(str(rec.get("id")), code)
    return out


def own_handled_ids() -> dict[str, str]:
    """{id: источник} — вакансии, которые ТЕКУЩИЙ аккаунт уже трогал: свой журнал, очередь и
    статусы. Дедуп приоритетного аккаунта (RFC-004) против СВОЕЙ истории — вместо общих
    `marks.json`, где в основном отклики основного аккаунта (их acc2 как раз и добирает).
    Статусы покрывают и ручные отклики: раз есть чат/метка работодателя — отклик БЫЛ."""
    out: dict[str, str] = {}
    for rec in followup.load_applied_log():          # свой applied_log.jsonl
        out.setdefault(str(rec.get("id")), "applied")
    for rec in followup.load_pending():              # своя очередь ожидания (лента -> крон)
        out.setdefault(str(rec.get("id")), "pending")
    for vid in followup.load_statuses():             # свои статусы (отклик/интервью/отказ/…)
        out.setdefault(str(vid), "status")
    return out
