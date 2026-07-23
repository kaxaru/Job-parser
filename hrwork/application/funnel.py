"""Воронка откликов и латентность автоотказов — аналитика над CRM-состоянием.

Только ЧТЕНИЕ: applied_log (время отклика) + chat_messages (время отказа) +
response_status (исход) + репозиторий (работодатель). Боевой путь откликов не трогает.

Гипотеза «автобан»: чем быстрее пришёл отказ после отклика, тем вероятнее, что резюме
никто не читал — отбил ATS-фильтр. Меряем латентность отказа по бакетам <=10м/<=1ч/<=1д
и агрегируем по компаниям + общую воронку.

Исход вакансии — ТОЛЬКО по статусу HH (chat.DISCARD_STATES / INVITED_STATES), этажи
взаимоисключающие: applied = rejected + invited + no_outcome. Reject-сообщение в чате
даёт лишь ВРЕМЯ отказа; без подтверждающего статуса оно не считается отказом — у
classify известные ложные срабатывания на проходное «к сожалению» (fix.md №2/№3),
такие чаты видны отдельным счётчиком chat_reject_only.
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
from pathlib import Path

from hrwork.application.apply.chat.chat import DISCARD_STATES, INVITED_STATES
from hrwork.application.apply.chat.chat_class import ChatKind, classify
from hrwork.application.apply.runtime.store import store
from hrwork.infrastructure.storage import vacancy_repository

# Наивные легаси-метки журнала писались локальным now() ЭТОЙ машины — берём её зону
# (самарская UTC+4), а не хардкод МСК: иначе латентность съезжает на час (fix.md №1).
_LOCAL_TZ = dt.datetime.now().astimezone().tzinfo
_RATE_MIN_N = 3     # автобан-доля ранжирует только компании с >=N измеренных отказов


def _parse_ts(s: str) -> dt.datetime | None:
    """ISO -> aware datetime (наивную метку трактуем как локальную зону машины)."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=_LOCAL_TZ) if d.tzinfo is None else d


def _reject_event(messages: list[dict]) -> dt.datetime | None:
    """ts первого сообщения РАБОТОДАТЕЛЯ, классифицированного как отказ; нет -> None."""
    for m in messages or []:
        if m.get("mine"):
            continue
        if classify(m.get("text") or "") is ChatKind.REJECT:
            return _parse_ts(m.get("ts") or "")
    return None


def _earliest_applied() -> dict[str, dict]:
    """Журнал -> {id: запись с САМЫМ РАННИМ ts}. Дубли id в append-only журнале бывают;
    ранний ts = реальный момент отклика (как в marks.js::applyJournal — fix.md №7)."""
    out: dict[str, dict] = {}
    for r in store.applied_log():
        vid, ts = r["id"], r.get("ts") or ""
        cur = out.get(vid)
        if cur is None or (ts and (not cur.get("ts") or ts < cur["ts"])):
            out[vid] = r
    return out


def compute_funnel() -> dict:
    """Воронка: на вакансию -> исход + латентность отказа; агрегация по компаниям + общая."""
    applied = _earliest_applied()
    statuses = store.statuses()                  # id -> employerState
    chats = store.chat_messages()                # id -> {messages, ...}
    employer_of = {r.id: (r.vacancy.employer or "") for r in vacancy_repository().load()}

    # ── на вакансию: исход + латентность отказа (минуты) ──
    per_company: dict[str, dict] = {}
    overall = {"applied": len(applied), "with_chat": 0, "rejected": 0,
               "le_10m": 0, "le_1h": 0, "le_1d": 0, "slow_reject": 0,
               "invited": 0, "no_outcome": 0, "chat_reject_only": 0, "latencies": []}

    for vid, rec in applied.items():
        # работодатель: из выдачи, иначе из журнала (пишется в момент отклика — fix.md №9);
        # старые записи без employer остаются «(вне выдачи)»
        emp = employer_of.get(vid) or rec.get("employer") or "(вне выдачи)"
        c = per_company.setdefault(emp, {"applied": 0, "rejected": 0, "le_10m": 0,
                                         "le_1h": 0, "le_1d": 0, "invited": 0,
                                         "measured": 0, "lat": []})
        c["applied"] += 1
        msgs = (chats.get(vid) or {}).get("messages") or []
        if msgs:
            overall["with_chat"] += 1
        st = statuses.get(vid)
        rej_ts = _reject_event(msgs)
        if st in DISCARD_STATES:                 # исход — только по статусу HH
            c["rejected"] += 1
            overall["rejected"] += 1
        elif st in INVITED_STATES:
            c["invited"] += 1
            overall["invited"] += 1
            continue
        else:
            overall["no_outcome"] += 1           # исхода нет: молчат или диалог идёт
            if rej_ts is not None:               # reject-фраза без статуса-отказа —
                overall["chat_reject_only"] += 1  # вероятный false positive classify
            continue
        apply_ts = _parse_ts(rec.get("ts") or "")
        if rej_ts and apply_ts:
            lat_min = (rej_ts - apply_ts).total_seconds() / 60
            if lat_min < 0:                      # перекос меток — латентность недостоверна
                continue
            c["lat"].append(lat_min)
            c["measured"] += 1                   # отказов с измеримой латентностью (был чат-отказ)
            overall["latencies"].append(lat_min)
            for key, lim in (("le_10m", 10), ("le_1h", 60), ("le_1d", 1440)):
                if lat_min <= lim:
                    c[key] += 1
                    overall[key] += 1
            if lat_min > 1440:
                overall["slow_reject"] += 1

    # ── таблица по компаниям; автобан-доля — от ИЗМЕРЕННЫХ отказов (fix.md №8) ──
    companies = []
    for emp, c in per_company.items():
        med = round(statistics.median(c["lat"])) if c["lat"] else None
        auto_rate = round(100 * c["le_1h"] / c["measured"], 1) if c["measured"] else 0.0
        companies.append({
            "employer": emp, "applied": c["applied"], "rejected": c["rejected"],
            "le_10m": c["le_10m"], "le_1h": c["le_1h"], "le_1d": c["le_1d"],
            "measured": c["measured"], "invited": c["invited"],
            "median_min": med, "auto_reject_rate": auto_rate,
        })
    # единое ранжирование (CSV и график): сперва компании с достаточной выборкой
    # (>=_RATE_MIN_N измеренных) по автобан-доле — 1/1=100 % больше не возглавляет топ
    companies.sort(key=lambda r: (r["measured"] >= _RATE_MIN_N, r["auto_reject_rate"],
                                  r["le_1h"], r["applied"]), reverse=True)

    overall["measured"] = len(overall["latencies"])
    overall["median_reject_min"] = (round(statistics.median(overall["latencies"]))
                                    if overall["latencies"] else None)
    overall.pop("latencies")
    return {"overall": overall, "companies": companies}


_CSV_HEADERS = ["Компания", "Откликов", "Отказов", "Измерено", "<=10м", "<=1ч",
                "<=1д", "Приглашений", "Медиана мин", "Автобан %"]


def write_funnel_csv(path: Path) -> dict:
    """Считает воронку -> CSV по компаниям (только с >=1 отказом) в `path`.
    Возвращает overall (для подписи графика/лога). Кодировка utf-8-sig — как у прочих
    отчётов. Порядок строк = ранжирование compute_funnel — график читает его как есть."""
    data = compute_funnel()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(_CSV_HEADERS)
        for c in data["companies"]:
            if not c["rejected"]:
                continue                         # без отказов в воронке автобана нечего показывать
            wr.writerow([c["employer"], c["applied"], c["rejected"], c["measured"],
                         c["le_10m"], c["le_1h"], c["le_1d"], c["invited"],
                         c["median_min"] if c["median_min"] is not None else "",
                         c["auto_reject_rate"]])
    return data["overall"]
