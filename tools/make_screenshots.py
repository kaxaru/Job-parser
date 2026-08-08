"""Пересобрать скриншоты README: docs/img/{feed,dashboard}-{dark,light}.png.

    ..\\.venv3\\Scripts\\python.exe tools\\make_screenshots.py

Зачем скрипт, а не «сделать кадр руками»: до 08.08.2026 картинки были сняты вручную
27.07 и протухли молча — на них ещё 4 портала из девяти. Пересборка одной командой
делает их обновляемыми вместе с кодом.

ЧТО ПОПАДАЕТ В КАДР. Вакансии — настоящие, это публичная выдача порталов. CRM-СЛОЙ
СИНТЕТИЧЕСКИЙ И ГЕНЕРИТСЯ ЗДЕСЬ: отметки, статусы откликов, переписка и воронка
собираются детерминированным `random.Random(SEED)` поверх реальных вакансий. Настоящая
история откликов (`data/marks.json`, статусы, чаты с рекрутерами) в репозиторий не
попадает — это обещание README, и держит его именно эта подмена.

Браузер поднимается со СВЕЖИМ временным профилем, `data/browser_profile/` не трогается:
persistent-профиль Chromium ломается от второго процесса (CLAUDE.md).
"""
from __future__ import annotations

import contextlib
import datetime
import random
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hrwork.application import funnel
from hrwork.application.apply.runtime.store import store
from hrwork.config import log
from hrwork.infrastructure.storage import vacancy_repository
from hrwork.presentation.views import dashboard, feed

SEED = 20260808
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "img"
# Сколько вакансий кладём в демо-ленту. Полный кеш (~105 тыс.) дал бы feed-data.js на 58 МБ:
# скриншоту это не нужно, а страница грузилась бы минутами. Срез берём с шагом по всему
# кешу, а не первые N подряд, — иначе в кадр попал бы один портал и один город.
DEMO_SIZE = 4_000
MIN_PER_SOURCE = 30           # даже крошечный портал обязан попасть в фильтры на кадре
VIEWPORT = {"width": 1_600, "height": 1_000}
SCALE = 2                     # retina-плотность: текст на PNG не мылится

# Демо-CRM: правдоподобные доли, а не «всё подряд отвечено».
_STATES = ["RESPONSE", "RESPONSE", "RESPONSE", "DISCARD", "DISCARD",
           "DISCARD_BY_EMPLOYER", "CONSIDER", "INVITATION", "PHONE_INTERVIEW", "INTERVIEW"]
_HR_LINES = [
    "Здравствуйте! Спасибо за отклик, изучаем резюме и вернёмся с ответом на этой неделе.",
    "Добрый день! Расскажите, пожалуйста, про опыт с асинхронным Python.",
    "Здравствуйте! Готовы пригласить на техническое интервью — какие слоты вам удобны?",
]
_BOT_LINE = "Ваш отклик доставлен работодателю. Мы сообщим об изменении статуса."


def _demo_records() -> list[Any]:
    """Срез реальных вакансий: пропорционально по порталам, минимум MIN_PER_SOURCE с каждого.

    Равномерный шаг по всему кешу здесь НЕ работает: `[::step][:DEMO_SIZE]` резал хвост,
    а jobicy лежит в кеше последним — на первом кадре 08.08 его портала не было вовсе,
    хотя смысл скриншота — «все 9 источников». Стратификация даёт каждому порталу место."""
    all_recs = vacancy_repository().load()
    by_src: dict[str, list[Any]] = {}
    for r in all_recs:
        by_src.setdefault(r.vacancy.source, []).append(r)
    chosen: set[str] = set()
    for recs_of in by_src.values():
        quota = max(MIN_PER_SOURCE, DEMO_SIZE * len(recs_of) // len(all_recs))
        step = max(1, len(recs_of) // quota)
        chosen.update(r.id for r in recs_of[::step][:quota])
    recs = [r for r in all_recs if r.id in chosen]     # исходный порядок кеша сохраняем
    log.info("демо-срез: {} вакансий из {}, порталов {}: {}",
             len(recs), len(all_recs), len(by_src),
             {s: sum(1 for r in recs if r.vacancy.source == s) for s in by_src})
    return recs


def _demo_crm(records: list[Any]) -> dict[str, Any]:
    """Синтетический CRM-слой поверх среза: отметки, статусы, чаты, журнал откликов."""
    rnd = random.Random(SEED)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    ids = [r.id for r in records]
    # Первые карточки — с откликами гарантированно: кадр снимается по верху ленты, а на
    # случайной выборке из 4000 бейджи статуса и переписки в него просто не попадали.
    head = ids[:24]
    applied_ids = head + [i for i in rnd.sample(ids, k=min(len(ids) // 8, 400))
                          if i not in set(head)]

    marks: dict[str, str] = {}
    statuses: dict[str, str] = {}
    chats: dict[str, Any] = {}
    journal: list[dict[str, Any]] = []

    for i, vid in enumerate(applied_ids):
        rec = next(r for r in records if r.id == vid)
        ts = (now - datetime.timedelta(days=rnd.randint(0, 25),
                                       minutes=rnd.randint(0, 1_440))).isoformat()
        journal.append({"id": vid, "name": rec.vacancy.name, "url": rec.url,
                        "via": "cron", "status": "applied", "ts": ts})
        state = _STATES[i % len(_STATES)]
        statuses[vid] = state
        marks[vid] = "rejected" if state.startswith("DISCARD") else "applied"
        msgs: list[dict[str, Any]] = [
            {"text": f"Здравствуйте! Заинтересовала вакансия {rec.vacancy.name}.",
             "mine": True, "ts": ts, "bot": False},
            {"text": _BOT_LINE, "mine": False, "ts": ts, "bot": True},
        ]
        if i % 3 == 0:                       # часть переписок с живым ответом рекрутера
            msgs.append({"text": _HR_LINES[i % len(_HR_LINES)], "mine": False,
                         "ts": (now - datetime.timedelta(days=rnd.randint(0, 6))).isoformat(),
                         "bot": False})
        chats[vid] = {"chatId": 5_000_000 + i, "messages": msgs,
                      "write": {"name": "ENABLED_FOR_ALL", "writeDisabledReasons": []}}

    forms = {vid: {"name": next(r for r in records if r.id == vid).vacancy.name,
                   "url": "", "ts": ""}
             for vid in rnd.sample(ids, k=min(len(ids) // 60, 40))}
    log.info("демо-CRM: откликов {}, чатов {}, анкет {}", len(journal), len(chats), len(forms))
    return {"marks": marks, "statuses": statuses, "chats": chats,
            "journal": journal, "forms": forms}


def _patch(mp: Any, out_data: Path, records: list[Any], crm: dict[str, Any]) -> None:
    """Перенаправить оба билдера в tmp и подменить ВСЕ читатели состояния.

    Держателей репозитория два — `feed`/`dashboard` и `funnel`: подмена только первого
    оставляла бы воронку на настоящих откликах (та же дыра, что нашлась в тестах 08.08)."""
    repo = type("_Repo", (), {"load": lambda _self: records})()
    for mod in (feed, dashboard, funnel):
        with contextlib.suppress(AttributeError):
            mp.setattr(mod, "vacancy_repository", lambda: repo)
    mp.setattr(store, "marks", lambda: dict(crm["marks"]))
    mp.setattr(store, "statuses", lambda: dict(crm["statuses"]))
    mp.setattr(store, "chat_messages", lambda: dict(crm["chats"]))
    mp.setattr(store, "applied_log", lambda: list(crm["journal"]))
    mp.setattr(store, "forms", lambda: dict(crm["forms"]))
    mp.setattr(store, "form_cache", dict)
    for mod in (feed, dashboard):
        mp.setattr(mod, "DATA_DIR", out_data)
    mp.setattr(feed, "FEED_OUT", out_data / "feed.html")
    mp.setattr(dashboard, "DASHBOARD_OUT", out_data / "dashboard.html")
    mp.setattr(dashboard, "REPORTS_DIR", out_data / "reports")


def _shoot(page: Any, url: str, theme: str, wait_for: str, out: Path) -> None:
    """Снять страницу в заданной теме. Тема — localStorage['feed.theme'], общий ключ
    у ленты и дашборда; ставим ДО загрузки, чтобы не поймать кадр на переключении."""
    page.add_init_script(f"localStorage.setItem('feed.theme', {theme!r});")
    page.goto(url, wait_until="load", timeout=120_000)
    page.wait_for_selector(wait_for, timeout=120_000)
    page.wait_for_timeout(2_500)              # догрузка ленивых панелей и шрифтов
    page.screenshot(path=str(out))
    log.success("{} -> {} КБ", out.name, out.stat().st_size // 1024)


def main() -> int:
    import pytest  # MonkeyPatch как готовый откатываемый патчер
    from playwright.sync_api import sync_playwright

    records = _demo_records()
    crm = _demo_crm(records)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hrwork_shots_") as tmp:
        out_data = Path(tmp) / "data"
        with pytest.MonkeyPatch.context() as mp:
            _patch(mp, out_data, records, crm)
            feed.build_feed()
            dashboard.build_dashboard()
        # plotly.js лежит в настоящем data/ (гитигнорен) — дашборд ссылается на него
        # относительным путём, поэтому кладём рядом, иначе графики уедут на CDN.
        for js in (Path(__file__).resolve().parent.parent / "data").glob("plotly-*.min.js"):
            shutil.copy(js, out_data / js.name)

        with sync_playwright() as p:
            browser = p.chromium.launch()     # свежий временный профиль, не data/browser_profile
            ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE,
                                      locale="ru-RU")
            for name, html, sel in (("feed", "feed.html", ".card"),
                                    ("dashboard", "dashboard.html", ".js-plotly-plot")):
                for theme in ("dark", "light"):
                    page = ctx.new_page()
                    _shoot(page, (out_data / html).as_uri(), theme, sel,
                           OUT_DIR / f"{name}-{theme}.png")
                    page.close()
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
