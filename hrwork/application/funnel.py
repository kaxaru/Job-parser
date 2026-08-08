"""Воронка откликов и латентность автоотказов — аналитика над CRM-состоянием.

Только ЧТЕНИЕ: applied_log (время отклика) + chat_messages (время отказа) +
response_status (исход) + репозиторий (работодатель). Боевой путь откликов не трогает.

Гипотеза «автобан»: чем быстрее пришёл отказ после отклика, тем вероятнее, что резюме
никто не читал — отбил ATS-фильтр. Меряем латентность отказа по бакетам <=10м/<=1ч/<=1д
и агрегируем по компаниям + общую воронку.

Исход вакансии — ТОЛЬКО по статусу HH (chat.DISCARD_STATES / INVITED_STATES), этажи
взаимоисключающие: applied = rejected + invited + no_outcome. Reject-сообщение в чате
даёт лишь ВРЕМЯ отказа (берётся ПОСЛЕДНЕЕ — см. `_reject_event`); без подтверждающего
статуса оно не считается отказом — у classify известные ложные срабатывания на проходное
«к сожалению» (fix.md №2/№3), такие чаты видны отдельным счётчиком chat_reject_only.
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
from pathlib import Path
from typing import Any

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


def _reject_event(messages: list[dict[str, Any]]) -> dt.datetime | None:
    """ts ПОСЛЕДНЕГО сообщения РАБОТОДАТЕЛЯ, классифицированного как отказ; нет -> None.

    Именно последнего, а не первого (баг 08.08.2026): у `classify` известные ложные
    срабатывания на проходное «удалёнки, к сожалению, нет» (fix.md №2/№3), и такая живая
    реплика через 5 минут после отклика уводила компанию в бакет мгновенного автобана,
    хотя настоящий отказ пришёл через двое суток. Момент смены статуса нам недоступен
    (`response_status.json` хранит только код состояния, без метки времени), поэтому из
    двух ошибок выбираем безопасную: поздний отказ занижает подозрение в ATS-бане,
    ранний — обвиняет компанию по чужой фразе.

    Берём максимум по разобранным меткам, а не последний элемент списка: порядок
    сообщений в кеше переписки контрактом не гарантирован."""
    stamps = [ts for m in messages or []
              if not m.get("mine") and classify(m.get("text") or "") is ChatKind.REJECT
              and (ts := _parse_ts(m.get("ts") or "")) is not None]
    return max(stamps) if stamps else None


def _earliest_applied() -> dict[str, dict[str, Any]]:
    """Журнал -> {id: запись с САМЫМ РАННИМ моментом отклика}. Дубли id в append-only
    журнале бывают; ранний ts = реальный момент отклика (как в marks.js::applyJournal —
    fix.md №7).

    Сравниваем РАЗОБРАННЫЕ метки, а не ISO-строки (баг 08.08.2026): в журнале сосуществуют
    смещения +04:00 (`append_applied` локальной машины) и +03:00 (метки HH), и строкой
    «09:30+03:00» (06:30Z) меньше «10:00+04:00» (06:00Z) — выбирался поздний отклик, а
    латентность отказа съезжала на бакет. Битая/пустая метка — данные с диска: она не
    роняет воронку и НЕ побеждает валидную (строкой «0000-99-99…» побеждала и лишала
    отклик измеримой латентности)."""
    out: dict[str, dict[str, Any]] = {}
    best: dict[str, dt.datetime | None] = {}
    for r in store.applied_log():
        vid, ts = r["id"], _parse_ts(r.get("ts") or "")
        if vid not in out:
            out[vid], best[vid] = r, ts
            continue
        cur = best[vid]
        if ts is not None and (cur is None or ts < cur):
            out[vid], best[vid] = r, ts
    return out


def compute_funnel() -> dict[str, Any]:
    """Воронка: на вакансию -> исход + латентность отказа; агрегация по компаниям + общая."""
    applied = _earliest_applied()
    statuses = store.statuses()                  # id -> employerState
    chats = store.chat_messages()                # id -> {messages, ...}
    employer_of = {r.id: (r.vacancy.employer or "") for r in vacancy_repository().load()}

    # ── на вакансию: исход + латентность отказа (минуты) ──
    # dict[str, Any], а не TypedDict: счётчики набираются по ВЫЧИСЛЯЕМОМУ ключу
    # (`for key, lim in (("le_10m", 10), ...): c[key] += 1`), а TypedDict требует
    # ключ-литерал. Значения разнородны (int + list[float]) — без аннотации mypy
    # выводит dict[str, object] и роняет `+= 1` на каждом счётчике.
    per_company: dict[str, dict[str, Any]] = {}
    overall: dict[str, Any] = {"applied": len(applied), "with_chat": 0, "rejected": 0,
                               "le_10m": 0, "le_1h": 0, "le_1d": 0, "slow_reject": 0,
                               "invited": 0, "no_outcome": 0, "chat_reject_only": 0,
                               "latencies": []}

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


# Две подписи правились 08.08.2026, обе — про однозначность для читателя таблицы:
# * «Позитив/в работе» вместо «Приглашений»: колонка считает INVITED_STATES, а туда входит
#   CONSIDER («Рассматривается») — это ещё не приглашение. Набор един с лентой НАМЕРЕННО
#   (fix.md №6), поэтому правилась подпись, а не набор.
# * «Автобан % (от измеренных)»: знаменатель — «Измерено», а не «Отказов». Тот же
#   знаменатель теперь и в шапке графика (`charts.py::chart_company_funnel`), где раньше
#   доля считалась от всех отказов: 2 из 2 измеренных давали «100.0» в таблице и «20 %»
#   в шапке той же вкладки.
_CSV_HEADERS = ["Компания", "Откликов", "Отказов", "Измерено", "<=10м", "<=1ч",
                "<=1д", "Позитив/в работе", "Медиана мин", "Автобан % (от измеренных)"]


def write_funnel_csv(path: Path) -> dict[str, Any]:
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
    overall: dict[str, Any] = data["overall"]
    return overall
