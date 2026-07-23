"""Воронка откликов и латентность автоотказов — аналитика над CRM-состоянием.

Только ЧТЕНИЕ: applied_log (время отклика) + chat_messages (время/автор отказа) +
response_status (исход) + репозиторий (работодатель). Боевой путь откликов не трогает.

Гипотеза «автобан»: чем быстрее пришёл отказ после отклика, тем вероятнее, что резюме
никто не читал — отбил ATS-фильтр. Меряем латентность отказа по бакетам <10м/<1ч/<1д и
агрегируем по компаниям + общую воронку.
"""
from __future__ import annotations

import datetime as dt
import statistics

from hrwork.application.apply.chat.chat_class import ChatKind, classify
from hrwork.application.apply.runtime.store import store
from hrwork.infrastructure.storage import vacancy_repository

_MSK = dt.timezone(dt.timedelta(hours=3))   # МСК = фикс. UTC+3 (без DST с 2014; tzdata не нужен)
# исходы «работодатель проявил интерес» (успех воронки)
_INVITED = {"INVITATION", "PHONE_INTERVIEW", "INTERVIEW", "ASSESSMENT", "HIRED", "CONSIDER"}
_DISCARD = {"DISCARD", "DISCARD_BY_EMPLOYER", "DISCARD_VACANCY_CLOSED"}


def _parse_ts(s: str) -> dt.datetime | None:
    """ISO -> aware datetime (наивные метки applied_log трактуем как МСК; иначе поедет на 3ч)."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=_MSK) if d.tzinfo is None else d


def _reject_event(messages: list[dict]) -> tuple[dt.datetime | None, bool]:
    """Первое сообщение РАБОТОДАТЕЛЯ с отказом -> (ts, is_bot). Нет отказа -> (None, False)."""
    for m in messages or []:
        if m.get("mine"):
            continue
        if classify(m.get("text") or "") is ChatKind.REJECT:
            return _parse_ts(m.get("ts") or ""), bool(m.get("bot"))
    return None, False


def compute_funnel() -> dict:
    """Воронка: на вакансию -> исход + латентность отказа; агрегация по компаниям + общая."""
    applied = {r["id"]: r for r in store.applied_log()}          # id -> {ts, name, ...}
    statuses = store.statuses()                                  # id -> employerState
    chats = store.chat_messages()                                # id -> {messages, ...}
    employer_of = {r.id: (r.vacancy.employer or "") for r in vacancy_repository().load()}

    # ── на вакансию: исход + латентность отказа (минуты) ──
    per_company: dict[str, dict] = {}
    overall = {"applied": len(applied), "with_chat": 0, "rejected": 0,
               "le_10m": 0, "le_1h": 0, "le_1d": 0, "slow_reject": 0,
               "invited": 0, "no_response": 0, "latencies": []}

    for vid, rec in applied.items():
        emp = employer_of.get(vid) or "(вне выдачи)"
        c = per_company.setdefault(emp, {"applied": 0, "rejected": 0, "le_10m": 0,
                                         "le_1h": 0, "le_1d": 0, "invited": 0,
                                         "measured": 0, "lat": []})
        c["applied"] += 1
        st = statuses.get(vid)
        if st in _INVITED:
            c["invited"] += 1
            overall["invited"] += 1
        msgs = (chats.get(vid) or {}).get("messages") or []
        if msgs:
            overall["with_chat"] += 1
        # отказ: статус DISCARD или reject-сообщение в чате
        rej_ts, _is_bot = _reject_event(msgs)
        is_reject = st in _DISCARD or rej_ts is not None
        if not is_reject:
            if st not in _INVITED:
                overall["no_response"] += 1        # отклик есть, работодатель молчит/не отказал
            continue
        c["rejected"] += 1
        overall["rejected"] += 1
        apply_ts = _parse_ts(rec.get("ts") or "")
        if rej_ts and apply_ts:
            lat_min = (rej_ts - apply_ts).total_seconds() / 60
            if lat_min < 0:                        # перекос меток — латентность недостоверна
                continue
            c["lat"].append(lat_min)
            c["measured"] += 1                     # отказов с измеримой латентностью (был чат-отказ)
            overall["latencies"].append(lat_min)
            for key, lim in (("le_10m", 10), ("le_1h", 60), ("le_1d", 1440)):
                if lat_min <= lim:
                    c[key] += 1
                    overall[key] += 1
            if lat_min > 1440:
                overall["slow_reject"] += 1

    # ── таблица по компаниям (только с ≥1 откликом; сортировка — автобан сверху) ──
    companies = []
    for emp, c in per_company.items():
        med = round(statistics.median(c["lat"])) if c["lat"] else None
        auto_rate = round(100 * c["le_1h"] / c["rejected"], 1) if c["rejected"] else 0.0
        companies.append({
            "employer": emp, "applied": c["applied"], "rejected": c["rejected"],
            "le_10m": c["le_10m"], "le_1h": c["le_1h"], "le_1d": c["le_1d"],
            "measured": c["measured"], "invited": c["invited"],
            "median_min": med, "auto_reject_rate": auto_rate,
        })
    # автобан-компании вперёд: сначала доля быстрых отказов, потом объём
    companies.sort(key=lambda r: (r["auto_reject_rate"], r["le_1h"], r["applied"]), reverse=True)

    overall["measured"] = len(overall["latencies"])
    overall["median_reject_min"] = (round(statistics.median(overall["latencies"]))
                                    if overall["latencies"] else None)
    overall.pop("latencies")
    return {"overall": overall, "companies": companies}


_CSV_HEADERS = ["Компания", "Откликов", "Отказов", "Измерено", "<=10м", "<=1ч",
                "<=1д", "Приглашений", "Медиана мин", "Автобан %"]


def write_funnel_csv(path) -> dict:
    """Считает воронку -> CSV по компаниям (только с >=1 отказом) в `path`.
    Возвращает overall (для подписи графика/лога). Кодировка utf-8-sig — как у прочих отчётов."""
    import csv

    data = compute_funnel()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(_CSV_HEADERS)
        for c in data["companies"]:
            if not c["rejected"]:
                continue                           # без отказов в воронке автобана нечего показывать
            wr.writerow([c["employer"], c["applied"], c["rejected"], c["measured"],
                         c["le_10m"], c["le_1h"], c["le_1d"], c["invited"],
                         c["median_min"] if c["median_min"] is not None else "",
                         c["auto_reject_rate"]])
    return data["overall"]
