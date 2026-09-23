"""Сводка CRM по всем аккаунтам (RFC-004): лента на localhost показывает, кто куда откликался.

`serve` работает от основного аккаунта и читает файлы ВСЕХ (`taken.py::account_dirs`). Журналы
объединяются с полем `account`, статусы и переписка — тоже (у каждой вакансии на карточке
может быть отклик от обоих аккаунтов). Всё только на чтение.
"""
import datetime
from pathlib import Path
from typing import Any

from hrwork.application.apply import account_session, taken
from hrwork.application.apply.runtime import quota
from hrwork.config import HH_APPLY_ROLLING_CAP
from hrwork.domain.account import MAIN_CODE
from hrwork.infrastructure.storage import followup, read_json_or


def _label(code: str) -> str:
    if code == MAIN_CODE:
        return "основной"
    meta = read_json_or(taken.account_dirs()[code] / "account.json", {})
    return str(meta.get("label") or code) if isinstance(meta, dict) else code


def _mtime_utc(path: Path) -> str | None:
    """Время последней записи файла (ISO, UTC) или None, если файла нет. UTC, а не местное:
    строку разбирает браузер и сам переводит в свой пояс; местная метка сервера сделала бы
    ответ зависимым от пояса машины, где крутится `serve`."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat(timespec="seconds")


def _pulse(rows: list[dict[str, Any]], folder: Path, now: datetime.datetime) -> dict[str, Any]:
    """«Отклики были? синк был? почему мало?» одной записью (панель профилей, 24.09.2026).

    Всё считается по журналу и mtime файлов, а не по `apply_quota.json`: квота привязана к
    аккаунту процесса (`quota.QUOTA_FILE` от `ACCOUNT_DIR`), а `serve` читает ВСЕ аккаунты.
    `today` — разные вакансии с местной датой отклика = сегодня; окно — тот же счёт, что у
    прогона (`quota.applied_in_window`), поэтому лента показывает ровно то, во что упрётся
    следующий слот."""
    stamps = [(str(r.get("id")), ts) for r in rows
              if (ts := quota.journal_ts(str(r.get("ts") or ""))) is not None]
    today = now.astimezone().date()
    free_at = quota.window_free_at(rows, HH_APPLY_ROLLING_CAP, moment=now)
    return {
        "today": len({vid for vid, ts in stamps if ts.astimezone().date() == today}),
        "window24": quota.applied_in_window(rows, moment=now),
        "window_cap": HH_APPLY_ROLLING_CAP,
        "window_free_at": free_at.isoformat() if free_at else None,
        "last_applied": max(ts for _, ts in stamps).isoformat() if stamps else None,
        "last_sync": _mtime_utc(folder / followup.RESPONSE_STATUS_FILE.name),
    }


def accounts(moment: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """[{code, label, session, session_checked, applied, …пульс}] по всем аккаунтам — для
    баннера, фильтра и панели профилей ленты.

    session — состояние входа (`session_status.json`): ok / expired / foreign / unknown,
    session_checked — когда прогон его в последний раз проверял (метка файла как есть).
    Основной свой статус тоже пишет с каждого прогона; «unknown» — файла ещё нет.
    Поля пульса — `_pulse`; `moment` — для тестов (по умолчанию «сейчас»)."""
    now = moment if moment is not None else datetime.datetime.now(datetime.timezone.utc)
    out = []
    for code, folder in taken.account_dirs().items():
        st = read_json_or(folder / account_session.SESSION_STATUS_FILE_NAME, {})
        rows = followup.load_applied_log(folder / followup.APPLIED_LOG_FILE.name)
        out.append({"code": code, "label": _label(code),
                    "session": str(st.get("state") or "unknown"),
                    "session_checked": st.get("ts") or None,
                    "applied": len(rows), **_pulse(rows, folder, now)})
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
