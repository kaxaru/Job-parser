"""Сводка CRM по всем аккаунтам (RFC-004): лента на localhost показывает, кто куда откликался.

`serve` работает от основного аккаунта и читает файлы ВСЕХ (`taken.py::account_dirs`). Журналы
объединяются с полем `account`, статусы и переписка — тоже (у каждой вакансии на карточке
может быть отклик от обоих аккаунтов). Всё только на чтение.
"""
from typing import Any

from hrwork.application.apply import account_session, taken
from hrwork.domain.account import MAIN_CODE
from hrwork.infrastructure.storage import followup, read_json_or


def _label(code: str) -> str:
    if code == MAIN_CODE:
        return "основной"
    meta = read_json_or(taken.account_dirs()[code] / "account.json", {})
    return str(meta.get("label") or code) if isinstance(meta, dict) else code


def accounts() -> list[dict[str, Any]]:
    """[{code, label, session, applied}] по всем аккаунтам — для баннера и фильтра ленты.

    session — состояние входа (`session_status.json`): ok / expired / foreign / unknown.
    Основной свой статус тоже пишет с каждого прогона; «unknown» — файла ещё нет."""
    out = []
    for code, folder in taken.account_dirs().items():
        st = read_json_or(folder / account_session.SESSION_STATUS_FILE_NAME, {})
        n = len(followup.load_applied_log(folder / followup.APPLIED_LOG_FILE.name))
        out.append({"code": code, "label": _label(code),
                    "session": str(st.get("state") or "unknown"), "applied": n})
    return out


def applied() -> list[dict[str, Any]]:
    """Журналы всех аккаунтов одним списком; у каждой строки `account`. Легаси-строки без
    поля относятся к владельцу файла (журнал у каждого аккаунта свой)."""
    rows: list[dict[str, Any]] = []
    for code, folder in taken.account_dirs().items():
        for rec in followup.load_applied_log(folder / followup.APPLIED_LOG_FILE.name):
            rows.append({**rec, "account": rec.get("account") or code})
    return rows


def statuses() -> dict[str, dict[str, Any]]:
    """Статусы работодателя ПО АККАУНТАМ: {vid: {account: state}}.

    Не плоско: у вакансии, на которую откликнулись оба аккаунта, статусы РАЗНЫЕ (у второго —
    свой отказ/приглашение). Плоская карта с «побеждает основной» показывала бы под фильтром
    acc2 метку основного — то, на что жаловался владелец 15.09.2026. Фронт выбирает статус
    того профиля, что в фильтре (`model.js::effectiveStatus`)."""
    out: dict[str, dict[str, Any]] = {}
    for code, folder in taken.account_dirs().items():
        data = followup.load_statuses(folder / followup.RESPONSE_STATUS_FILE.name)
        if not isinstance(data, dict):
            continue
        for vid, state in data.items():
            out.setdefault(str(vid), {})[code] = state
    return out


def chat_messages() -> dict[str, dict[str, Any]]:
    """Переписка ПО АККАУНТАМ: {vid: {account: info}}.

    Не плоско (как раньше «побеждает основной»): у вакансии с откликом от обоих чат И ДАТА
    разные. Плоская main-wins-карта показывала под фильтром acc2 чат и дату основного — отсюда
    «отказ ТПО Прайд 19 дней назад» от main под фильтром acc2 (жалоба владельца 16.09.2026).
    Фронт выбирает чат профиля из фильтра (`model.js::effectiveChat`), при пустом фильтре —
    основной (носитель ленты)."""
    out: dict[str, dict[str, Any]] = {}
    for code, folder in taken.account_dirs().items():
        data = followup.load_chat_messages(folder / followup.CHAT_MESSAGES_FILE.name)
        if not isinstance(data, dict):
            continue
        for vid, value in data.items():
            out.setdefault(str(vid), {})[code] = value
    return out
