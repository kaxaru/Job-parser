"""Единая точка фиксации отклика (`store.commit_applied`; аудит 22.09.2026, §1).

Тройка «отметка -> квота -> журнал» была написана ТРИЖДЫ (`autoclick::_apply_batch`,
`autoclick::_apply_one_vacancy`, `forms::run`) и уже разошлась: feed-путь не писал `ab`.
Цена расхождения — класс инцидентов 18.07/28.07 «отклик ушёл, а учёт не дошёл»: недосчитанная
квота (упор в лимит HH) или строка журнала без владельца доли (ломает наблюдаемость RFC-004).

Здесь проверяются две вещи: ПОРЯДОК записи внутри точки и то, что все три боевых пути приходят
в неё с ОДНИМ И ТЕМ ЖЕ набором полей.
"""
import contextlib
import sys

import pytest

from hrwork.application.apply import autoclick, browser
from hrwork.application.apply.forms import forms
from hrwork.application.apply.outcome import ApplyChannel
from hrwork.application.apply.runtime import lock
from hrwork.application.apply.runtime.store import ApplicationStore

pytestmark = pytest.mark.usefixtures("apply_defaults")

CRON_APPLY = "1"
FEED_APPLY = "777"
FORM_APPLY = "111"


# ── Сама точка: порядок записей и возврат счётчика ──
@pytest.fixture
def facade(monkeypatch):
    """Фасад с перехваченными записями: видно ПОРЯДОК и состав полей."""
    s = ApplicationStore()
    calls: list[tuple] = []
    monkeypatch.setattr(s, "remove_form", lambda v: calls.append(("remove", v)))
    monkeypatch.setattr(s, "mark_applied", lambda v: calls.append(("mark", v)))
    monkeypatch.setattr(s, "bump_quota", lambda n: calls.append(("quota", n)) or 42)
    # строка журнала — VO: видно тем же порядком полей, что уходит на диск (`via` — код канала,
    # enum сюда не доезжает, его переводит граница `store.log_applied`; аудит 22.09.2026, §5)
    monkeypatch.setattr(s, "log_applied",
                        lambda row: calls.append(("log", row.vid, row.name, row.url,
                                                  {"via": row.via, "employer": row.employer,
                                                   "ab": row.ab})))
    return s, calls


def test_commit_applied_orders_marks_then_quota_then_journal(facade):
    # порядок — как в крон-батче: отметка обязана лечь на диск раньше всего (именно её
    # отсутствие 18.07 дало повторные отклики после kill)
    s, calls = facade
    assert s.commit_applied(CRON_APPLY, name="Python-разработчик",
                            url="https://hh.ru/vacancy/1", employer="Ромашка",
                            via=ApplyChannel.CRON) == 42          # возврат = applied_today()
    assert calls == [
        ("mark", "1"),
        ("quota", 1),
        ("log", "1", "Python-разработчик", "https://hh.ru/vacancy/1",
         {"via": "cron", "employer": "Ромашка", "ab": None}),
    ]


def test_commit_applied_drops_the_form_before_marking(facade):
    # снятие из очереди ПЕРЕД отметкой: иначе убитый посреди прогона процесс вернул бы уже
    # отправленную анкету в оборот
    s, calls = facade
    s.commit_applied(FORM_APPLY, name="Backend разработчик",
                     url="https://hh.ru/vacancy/111", employer="Ромашка",
                     via=ApplyChannel.CRON, drop_form=True)
    assert calls == [
        ("remove", "111"),
        ("mark", "111"),
        ("quota", 1),
        ("log", "111", "Backend разработчик", "https://hh.ru/vacancy/111",
         {"via": "cron", "employer": "Ромашка", "ab": None}),
    ]


# ── Три боевых пути приходят в точку с одним набором полей ──
@pytest.fixture
def three_paths(monkeypatch):
    """Крон-батч, лента и дренаж форм — на заглушках; вызовы `commit_applied` перехвачены."""
    commits: list[tuple[str, dict]] = []
    monkeypatch.setattr(autoclick.store, "commit_applied",
                        lambda vid, **kw: commits.append((vid, kw)) or 1)

    # 1) крон-батч: пул из одной вакансии, все внешние вызовы — заглушки
    pool = [autoclick.Candidate(id=CRON_APPLY, name="Вакансия 1",
                                url="https://hh.ru/vacancy/1", employer="Ромашка")]
    monkeypatch.setattr(autoclick, "pick_candidates", lambda *a, **k: pool)
    monkeypatch.setattr(autoclick, "vacancy_repository",
                        lambda: type("R", (), {"load": staticmethod(list)})())
    monkeypatch.setattr(autoclick, "skippable_form_ids", lambda *a, **k: set())
    monkeypatch.setattr(autoclick, "_remember_vacancy_meta", lambda records: None)
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 0)
    monkeypatch.setattr(autoclick.store, "applied_log", list)   # журнал пуст -> квота не правится
    monkeypatch.setattr(autoclick.store, "forms", dict)
    monkeypatch.setattr(autoclick.store, "form_cache", dict)
    monkeypatch.setattr(autoclick.ab_split, "current_split", lambda: None)
    monkeypatch.setattr(autoclick.ab_split, "write_eligible", lambda ids: None)
    monkeypatch.setattr(autoclick.taken, "taken_by_others", dict)
    monkeypatch.setattr(autoclick, "apply_one",
                        lambda page, cand, **kw: autoclick.ApplyOutcome.APPLIED)
    monkeypatch.setattr(autoclick, "_send_cover_via_chat", lambda *a, **k: True)
    monkeypatch.setattr(autoclick.time, "sleep", lambda s: None)
    # 2) лента: работодателя в кеше нет — он приходит аргументом
    monkeypatch.setattr(autoclick, "_VACANCY_META", {})

    # 3) дренаж форм: очередь из одной анкеты, браузер и заполнение — заглушки
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        type(sys)("playwright.sync_api"))
    sys.modules["playwright.sync_api"].sync_playwright = contextlib.nullcontext
    monkeypatch.setattr(forms, "FORMS_ENABLED", True)
    monkeypatch.setattr(forms, "try_autofill", lambda *a, **k: True)
    monkeypatch.setattr(forms, "_open_form", lambda *a: None)
    monkeypatch.setattr(forms.time, "sleep", lambda *a: None)
    monkeypatch.setattr(forms.store, "forms",
                        lambda: {FORM_APPLY: {"name": "Backend разработчик",
                                              "url": "https://hh.ru/vacancy/111"}})
    monkeypatch.setattr(forms.store, "applied_ids", set)
    monkeypatch.setattr(forms.store, "marks", dict)
    monkeypatch.setattr(forms, "_VAC_CTX",
                        {FORM_APPLY: ("ООО Пример", "", "Backend разработчик",
                                      None, "between1And3")})
    monkeypatch.setattr(lock, "_single_instance", contextlib.nullcontext)
    monkeypatch.setattr(browser, "_launch", lambda *a, **k: object())
    monkeypatch.setattr(browser, "_page", lambda *a, **k: _RunPage())
    monkeypatch.setattr(browser, "_session_state",
                        lambda *_a: browser.LoginState.LOGGED_IN)
    monkeypatch.setattr(browser, "_goto", lambda *_a: True)
    monkeypatch.setattr(browser, "is_captcha", lambda *_a: False)
    return commits


class _RunPage:
    """Страница дренажа форм: важны только три настройки/паузы (DOM не трогается)."""

    def set_default_navigation_timeout(self, *_a):
        pass

    def set_default_timeout(self, *_a):
        pass

    def wait_for_timeout(self, *_a):
        pass


def test_the_three_paths_commit_the_same_field_set(three_paths):
    autoclick._apply_batch(page=None, apply_limit=1, daily_cap=200)
    autoclick._apply_one_vacancy(None, FEED_APPLY, "https://hh.ru/vacancy/777", "",
                                 name="Python-разработчик", employer="Сбер. IT")
    forms.run()

    assert [vid for vid, _ in three_paths] == ["1", "777", "111"]
    assert three_paths == [
        ("1", {"name": "Вакансия 1", "url": "https://hh.ru/vacancy/1", "employer": "Ромашка",
               "via": ApplyChannel.CRON, "ab": None}),
        ("777", {"name": "Python-разработчик", "url": "https://hh.ru/vacancy/777",
                 "employer": "Сбер. IT", "via": ApplyChannel.FEED, "ab": None}),
        ("111", {"name": "Backend разработчик", "url": "https://hh.ru/vacancy/111",
                 "employer": "ООО Пример", "via": ApplyChannel.CRON,
                 "drop_form": True, "ab": None}),
    ]
    # набор полей одинаков у всех трёх путей: разойтись по составу записи больше нечему
    # (расхождение и было находкой: feed-путь не писал `ab` вовсе)
    assert [{k for k in kw if k != "drop_form"} for _, kw in three_paths] == [
        {"name", "url", "employer", "via", "ab"},
        {"name", "url", "employer", "via", "ab"},
        {"name", "url", "employer", "via", "ab"},
    ]
