"""Тесты отбора кандидатов, дневной квоты и single-instance lock (без браузера)."""
import datetime
import json
import os
import subprocess
import sys

import pytest

from hrwork.application.apply import autoclick
from hrwork.application.apply.autoclick import pick_candidates
from hrwork.application.apply.candidates import ApplyTier
from hrwork.application.apply.runtime import lock, quota
from hrwork.application.apply.runtime.quota import applied_today
from hrwork.infrastructure.storage import JsonVacancyRepository


def _raw(vid="1", name="Python Backend разработчик", sched="remote",
         exp="between1And3", req="FastAPI, Docker, удалённая работа", created_at=None,
         employer="Acme", city="Москва"):
    # pick_candidates принимает VacancyRecord -> строим канонический raw-dict и прогоняем
    # через ACL репозитория (тот же путь, что боевой load): dict -> VacancyRecord.
    d = {
        "id": vid, "name": name,
        "area": {"id": "1", "name": city},
        "salary": None,
        "experience": {"id": exp},
        "schedule": {"id": sched},
        "snippet": {"requirement": req, "responsibility": ""},
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
        "employer": {"name": employer},
        "created_at": created_at,
        "_source": "hh",
    }
    return JsonVacancyRepository._from_dict(d)


def _created_days_ago(n: int) -> str:
    import datetime
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    return (now - datetime.timedelta(days=n)).isoformat()


def test_picks_matching_vacancy():
    out = pick_candidates([_raw()], marks={}, limit=10)
    assert [c.id for c in out] == ["1"]
    assert out[0].url == "https://hh.ru/vacancy/1"


def test_skips_already_marked():
    assert pick_candidates([_raw()], marks={"1": "applied"}, limit=10) == []
    assert pick_candidates([_raw()], marks={"1": "rejected"}, limit=10) == []


def test_skips_wrong_experience():
    assert pick_candidates([_raw(exp="moreThan6")], marks={}, limit=10) == []


# не-инженерные роли, формально проходящие как IT (Python в JD -> tier STRICT)
@pytest.mark.parametrize("name", [
    "Риск-аналитик (проект ПВР)",
    "Риск-менеджер по аналитике ПВР",
    "Портфельный аналитик/портфельный менеджер",
    "Аналитик по планированию и управлению ресурсами",
])
def test_skips_non_engineering_roles(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


# целимся в РОЛЬ, не в домен: разработчик в рисках — настоящая dev-вакансия
@pytest.mark.parametrize("name", [
    "Разработчик Python, группа по управлению рисками",
    "Разработчик моделей оценки кредитных рисков",
    "Аналитик данных",
    "Системный аналитик",
])
def test_keeps_dev_roles_in_same_domain(name):
    assert len(pick_candidates([_raw(name=name)], marks={}, limit=10)) == 1


# роль «Аналитик»: берём только айтишные подтипы, доменных пропускаем
@pytest.mark.parametrize("name", [
    "Аналитик данных", "Системный аналитик", "Data Analyst",
    "Дата-аналитик", "Аналитик данных (Junior)",
])
def test_analyst_it_subtypes_pass(name):
    assert len(pick_candidates([_raw(name=name)], marks={}, limit=10)) == 1


@pytest.mark.parametrize("name", [
    "Финансовый аналитик", "Логист-аналитик", "Медицинский аналитик",
    "Консультант-аналитик", "Специалист-аналитик отдела закупок", "Аналитик-экономист",
])
def test_analyst_domain_subtypes_skipped(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


def test_skips_form_vacancies_without_touching_marks():
    # вакансия-опросник: исключается по форм-очереди, а НЕ через marks -> в ленте она
    # остаётся с бейджем «форма» и НЕ помечается отказом
    marks = {}
    assert pick_candidates([_raw()], marks, limit=10, form_ids={"1"}) == []
    assert marks == {}                                  # marks не тронуты
    # без форм-очереди та же вакансия берётся как обычно
    assert len(pick_candidates([_raw()], marks, limit=10)) == 1


def test_skips_non_it_role():
    # не-IT вакансия (поддержка/крауд) с Python+remote+junior -> отсекается гардом is_it
    raw = _raw(name="Специалист поддержки пользователей", req="Python, удалённая работа")
    assert pick_candidates([raw], marks={}, limit=10) == []


def test_skips_office_in_non_tier3_city():
    # офис (нет remote/маркеров) в городе НЕ из APPLY_OFFICE_CITIES -> ни один tier не берёт
    raw = _raw(sched="fullDay", req="FastAPI, Docker, работа в офисе", city="Новосибирск")
    assert pick_candidates([raw], marks={}, limit=10) == []


def test_tier3_office_in_allowed_city():
    # офис + строгий стек + город из APPLY_OFFICE_CITIES -> OFFICE (берём)
    raw = _raw(sched="fullDay", req="FastAPI, Docker, работа в офисе", city="Самара")
    out = pick_candidates([raw], marks={}, limit=10)
    assert len(out) == 1 and out[0].tier is ApplyTier.OFFICE


def test_tier2_wide_stack_remote():
    # удалёнка, без Python/FastAPI, но с широким стеком (Django) -> WIDE
    raw = _raw(name="Backend разработчик", req="Django, PostgreSQL, удалённая работа")
    out = pick_candidates([raw], marks={}, limit=10)
    assert len(out) == 1 and out[0].tier is ApplyTier.WIDE


def test_tier_priority_strict_then_wide_then_office():
    strict = _raw(vid="s", req="FastAPI, удалённая работа")                       # tier1
    wide   = _raw(vid="w", name="Backend разработчик", req="Django, удалёнка")    # tier2
    office = _raw(vid="o", sched="fullDay", req="FastAPI, офис", city="Москва")   # tier3
    ids = [c.id for c in pick_candidates([office, wide, strict], marks={}, limit=10)]
    assert ids == ["s", "w", "o"]                  # строгий -> широкий -> офис


def test_accepts_remote_by_text_marker():
    # schedule офисный, но в тексте есть маркер удалёнки — как в ленте (remote_any)
    raw = _raw(sched="fullDay", req="FastAPI, возможна удалённая работа")
    assert len(pick_candidates([raw], marks={}, limit=10)) == 1


def test_skips_without_core_tech():
    raw = _raw(name="Java разработчик", req="Spring, Kotlin, удалённая работа")
    assert pick_candidates([raw], marks={}, limit=10) == []


def test_respects_limit():
    raws = [_raw(vid=str(i)) for i in range(5)]
    assert len(pick_candidates(raws, marks={}, limit=2)) == 2


def test_skips_ghost_vacancy():
    ghost = _raw(vid="9", created_at=_created_days_ago(90))   # >60 дн — гост
    assert pick_candidates([ghost], marks={}, limit=10) == []


def test_keeps_fresh_and_undated():
    fresh = _raw(vid="1", created_at=_created_days_ago(5))
    undated = _raw(vid="2", created_at=None)                  # старый кеш — не отсеиваем
    ids = {c.id for c in pick_candidates([fresh, undated], marks={}, limit=10)}
    assert ids == {"1", "2"}


@pytest.mark.parametrize("name", [
    "Senior Python разработчик", "ML инженер (Python)", "Python / MLOps engineer",
    "Python ML-разработчик", "Ведущий backend-разработчик Python",
    "Старший Python-разработчик", "Python Team Lead", "Python Backend (Lead)",
    "Тимлид Python",
])
def test_blacklist_ml_mlops_senior(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


def test_blacklist_does_not_hit_html_xml():
    # \bml\b не должен цеплять HTML/XML в тайтле
    ok = _raw(name="Python разработчик (HTML/XML парсеры)")
    assert len(pick_candidates([ok], marks={}, limit=10)) == 1


def test_skips_other_language_in_title_even_if_python_in_body():
    # Java-вакансия с Python «как плюс» в требованиях — Python попадёт в techs и пройдёт
    # core-фильтр, но другой язык в ТАЙТЛЕ должен отсеять («ищу только python»).
    for bad in ("Java разработчик", "C# Backend Developer", "PHP-программист",
                "Golang разработчик", "1С-программист"):
        raw = _raw(name=bad, req="Java, Spring, Python как плюс, удалённая работа")
        assert pick_candidates([raw], marks={}, limit=10) == [], bad


def test_keeps_python_title_with_other_lang_in_body():
    # Python в тайтле -> берём, даже если в теле упомянут Java
    ok = _raw(name="Python разработчик", req="FastAPI, опыт с Java будет плюсом, удалёнка")
    assert len(pick_candidates([ok], marks={}, limit=10)) == 1


def test_priority_no_experience_before_1_3():
    j = _raw(vid="j", exp="noExperience", created_at=_created_days_ago(40))
    m = _raw(vid="m", exp="between1And3", created_at=_created_days_ago(2))
    ids = [c.id for c in pick_candidates([m, j], marks={}, limit=10)]
    assert ids == ["j", "m"]                       # без опыта раньше, несмотря на возраст


def test_priority_fresher_first_within_same_exp():
    old = _raw(vid="old", exp="between1And3", created_at=_created_days_ago(50))
    new = _raw(vid="new", exp="between1And3", created_at=_created_days_ago(3))
    ids = [c.id for c in pick_candidates([old, new], marks={}, limit=10)]
    assert ids == ["new", "old"]                   # свежее раньше при равном опыте


def test_candidate_carries_employer_and_desc():
    c = pick_candidates([_raw(employer="Сбер. IT")], marks={}, limit=10)[0]
    assert c.employer == "Сбер. IT"
    assert "FastAPI" in c.desc


# ── Дневная квота откликов ──
@pytest.fixture
def tmp_quota(tmp_path, monkeypatch):
    f = tmp_path / "apply_quota.json"
    monkeypatch.setattr(quota, "QUOTA_FILE", f)
    monkeypatch.setattr(quota, "DATA_DIR", tmp_path)
    return f


def test_quota_empty_is_zero(tmp_quota):
    assert applied_today() == 0


def test_quota_accumulates_within_day(tmp_quota):
    assert quota.bump_quota(3) == 3
    assert quota.bump_quota(2) == 5
    assert applied_today() == 5


def test_quota_ignores_other_day(tmp_quota):
    tmp_quota.write_text(json.dumps({"date": "2000-01-01", "count": 199}), encoding="utf-8")
    assert applied_today() == 0                 # запись за прошлый день -> 0


# ── Кулдаун поднятия резюме (гейт слитой задачи bump+apply) ──
@pytest.fixture
def tmp_bump(tmp_path, monkeypatch):
    from hrwork.application.apply.runtime import bump_state
    monkeypatch.setattr(bump_state, "BUMP_FILE", tmp_path / "bump_state.json")
    return bump_state


def test_bump_due_when_never_bumped(tmp_bump):
    assert tmp_bump.bump_due() is True            # ни разу не поднимали -> пора
    assert tmp_bump.hours_since_bump() is None


def test_bump_not_due_right_after_bump(tmp_bump):
    tmp_bump.mark_bumped()
    assert tmp_bump.bump_due() is False           # только что подняли -> кулдаун
    assert tmp_bump.hours_since_bump() < 1


def test_bump_due_after_cooldown(tmp_bump):
    import datetime
    old = (datetime.datetime.now().astimezone()
           - datetime.timedelta(hours=tmp_bump.BUMP_COOLDOWN_H + 1)).isoformat(timespec="seconds")
    tmp_bump.BUMP_FILE.write_text(json.dumps({"last_ok": old}), encoding="utf-8")
    assert tmp_bump.bump_due() is True            # 4ч+ прошло -> снова пора


def test_bump_due_on_broken_state(tmp_bump):
    tmp_bump.BUMP_FILE.write_text("{битый", encoding="utf-8")
    assert tmp_bump.bump_due() is True            # битый файл не должен блокировать поднятие


# ── Single-instance lock ──
@pytest.fixture
def tmp_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "LOCK_FILE", tmp_path / "autoclick.lock")
    monkeypatch.setattr(lock, "DATA_DIR", tmp_path)
    return tmp_path / "autoclick.lock"


def test_lock_acquire_release(tmp_lock):
    with lock._single_instance():
        assert tmp_lock.exists()                # держим -> файл есть
    assert not tmp_lock.exists()                # вышли -> снят


def test_lock_rejects_fresh_live_instance(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    tmp_lock.write_text(json.dumps({"pid": 424242, "ts": lock.time.time()}), encoding="utf-8")
    with pytest.raises(SystemExit), lock._single_instance():
        pass


def test_lock_reclaims_stale(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    old = lock.time.time() - (lock.LOCK_TTL + 10)
    tmp_lock.write_text(json.dumps({"pid": 424242, "ts": old}), encoding="utf-8")
    with lock._single_instance():               # протух -> перезабираем
        assert json.loads(tmp_lock.read_text())["pid"] != 424242


# ── _lock_holder / ретрай ожидания lock (для крона) ──
def test_lock_holder_none_when_absent(tmp_lock):
    assert lock._lock_holder() is None


def test_lock_holder_reports_live_instance(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    tmp_lock.write_text(json.dumps({"pid": 42, "ts": lock.time.time()}), encoding="utf-8")
    held = lock._lock_holder()
    assert held and held[0] == 42


def test_lock_holder_none_when_stale(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    stale = lock.time.time() - lock.LOCK_TTL - 5
    tmp_lock.write_text(json.dumps({"pid": 42, "ts": stale}), encoding="utf-8")
    assert lock._lock_holder() is None


def test_lock_retry_waits_then_raises(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    sleeps = []
    monkeypatch.setattr(lock.time, "sleep", lambda s: sleeps.append(s))
    tmp_lock.write_text(json.dumps({"pid": 42, "ts": lock.time.time()}), encoding="utf-8")
    with pytest.raises(SystemExit), lock._single_instance(wait_retries=2, wait_s=1):
        pass
    assert len(sleeps) == 2              # выждал 2 попытки, затем сдался


def test_lock_retry_acquires_when_freed(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    tmp_lock.write_text(json.dumps({"pid": 42, "ts": lock.time.time()}), encoding="utf-8")
    monkeypatch.setattr(lock.time, "sleep", lambda s: tmp_lock.unlink())  # освободился «во сне»
    with lock._single_instance(wait_retries=3, wait_s=1):
        assert json.loads(tmp_lock.read_text())["pid"] != 42                   # взяли свой lock


def test_lock_holder_broken_json_recent_is_held(tmp_lock):
    # битый/недописанный lock со свежим mtime -> НЕ «свободен» (иначе второй браузер)
    tmp_lock.write_text("{not json", encoding="utf-8")
    held = lock._lock_holder()
    assert held is not None and held[0] == "?"


def test_lock_holder_broken_json_old_is_free(tmp_lock, monkeypatch):
    # битый lock со старым mtime -> осиротевший, отдаём
    tmp_lock.write_text("{not json", encoding="utf-8")
    old = lock.time.time() - lock.LOCK_TTL - 10
    os.utime(tmp_lock, (old, old))
    assert lock._lock_holder() is None


# ── Живость pid: осиротевший lock должен отдаваться СРАЗУ, а не по TTL ──
def test_pid_alive_true_for_running_process():
    assert lock._pid_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="zombie-pid — свойство хендлов Windows")
def test_pid_alive_false_for_killed_pid_while_handle_open():
    # ИНЦИДЕНТ 25.07.2026: watchdog расстрелял дерево, lock остался с pid=26652. OpenProcess
    # на мёртвый pid УСПЕВАЛ (объект жив, пока чужой хендл его держит) -> _pid_alive врал
    # «жив» -> автоотклики стояли 3ч до LOCK_TTL. Держим хендл сами, воспроизводя условие.
    import ctypes
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, p.pid)   # объект не исчезнет
    try:
        p.wait(timeout=30)
        assert lock._pid_alive(p.pid) is False
    finally:
        if h:
            ctypes.windll.kernel32.CloseHandle(h)


# ── release_if_mine: аварийная отдача lock перед сносом дерева ──
def test_release_if_mine_drops_own_lock(tmp_lock):
    tmp_lock.write_text(json.dumps({"pid": os.getpid(), "ts": lock.time.time()}), encoding="utf-8")
    assert lock.release_if_mine() is True
    assert not tmp_lock.exists()


def test_release_if_mine_keeps_foreign_lock(tmp_lock):
    # чужой lock не наш, чтобы снимать: иначе второй Chromium на persistent-профиль
    tmp_lock.write_text(json.dumps({"pid": 424242, "ts": lock.time.time()}), encoding="utf-8")
    assert lock.release_if_mine() is False
    assert tmp_lock.exists()


@pytest.mark.parametrize("body", [None, "{битый", '{"ts": 1}'])
def test_release_if_mine_survives_absent_broken_and_pidless(tmp_lock, body):
    if body is not None:
        tmp_lock.write_text(body, encoding="utf-8")
    assert lock.release_if_mine() is False           # ничего не сняли и не упали


def test_exit_keeps_lock_reclaimed_by_another_instance(tmp_lock):
    # Прогон, переживший LOCK_TTL, к выходу мог уже лишиться lock: другой инстанс счёл его
    # протухшим и перезабрал. Безусловный unlink в `finally` снёс бы ЧУЖОЙ файл -> второй
    # Chromium на persistent-профиль. Снимаем только свой.
    with lock._single_instance():
        tmp_lock.write_text(json.dumps({"pid": 424242, "ts": lock.time.time()}),
                            encoding="utf-8")          # lock «угнали» пока мы работали
    assert json.loads(tmp_lock.read_text())["pid"] == 424242      # чужой lock уцелел


def test_watchdog_releases_lock_before_killing_tree(monkeypatch):
    # ИНЦИДЕНТ 25.07.2026: снос дерева не давал сработать `finally` -> lock с мёртвым pid
    # блокировал крон-слоты откликов 3ч. Порядок обязателен: сперва отдать lock, потом стрелять.
    calls = []

    def _release():
        calls.append("release")
        return True

    monkeypatch.setattr(autoclick, "release_if_mine", _release)
    monkeypatch.setattr(autoclick.faulthandler, "dump_traceback", lambda **kw: None)
    monkeypatch.setattr(autoclick.subprocess, "run", lambda *a, **kw: calls.append("taskkill"))
    monkeypatch.setattr(autoclick.os, "_exit", lambda code: calls.append(f"exit{code}"))
    autoclick._kill_own_tree()
    assert calls == ["release", "taskkill", "exit1"]


# ── ApplyWorker: submit не виснет при падении браузера (дедлок) ──
def test_apply_worker_drains_on_launch_failure(monkeypatch):
    import contextlib
    w = autoclick.ApplyWorker(headless=True)
    monkeypatch.setattr(autoclick, "_single_instance", lambda **k: contextlib.nullcontext())

    def boom():
        raise RuntimeError("browser won't start")
    monkeypatch.setattr(autoclick, "_start_playwright", boom)

    res = w.submit("1", "https://hh.ru/vacancy/1", "cover", "name")
    assert res["status"] == "error"          # вернулся с ошибкой, НЕ завис


def test_apply_worker_busy_when_lock_taken(monkeypatch):
    w = autoclick.ApplyWorker(headless=True)

    def raise_busy(**_):
        raise SystemExit
    monkeypatch.setattr(autoclick, "_single_instance", raise_busy)
    res = w.submit("1", "https://hh.ru/vacancy/1", "cover", "name")
    assert res["status"] == "busy"           # lock занят кроном -> busy, не виснет


def test_get_apply_worker_singleton_thread_safe(monkeypatch):
    import threading
    autoclick._apply_worker = None
    created = []
    orig = autoclick.ApplyWorker

    class Counting(orig):
        def __init__(self, *a, **k):
            created.append(1)
            super().__init__(*a, **k)
    monkeypatch.setattr(autoclick, "ApplyWorker", Counting)

    workers = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        workers.append(autoclick.get_apply_worker())
    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(created) == 1                  # ровно один воркер, несмотря на гонку
    assert len({id(w) for w in workers}) == 1
    autoclick._apply_worker = None


# ── инкрементальный синк: «кеш vs сеть» по lastMessageTime из списка + терминальный статус ──
_NOW = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _chat_item(vid="1", ts="2026-07-01T10:00:00+03:00"):
    return {"chatId": 9, "vacancyId": vid, "applicantId": 7, "lastMessageTime": ts}


_CACHED = {"1": {"messages": [{"text": "отказ", "ts": "2026-07-01T10:00:00+03:00"}]}}


@pytest.mark.parametrize("ts, skip", [
    ("2026-07-01T10:00:00+03:00", True),    # сообщение 22 дня назад + DISCARD -> из кеша
    ("2026-07-22T10:00:00+03:00", False),   # вчера -> синкаем
    ("", False),                            # метки нет -> недоверие -> синкаем
    ("garbage", False),                     # битая метка -> синкаем
    ("2026-07-01T10:00:00", False),         # наивная метка (TypeError при сравнении) -> синкаем
])
def test_sync_cache_decision_by_last_message(ts, skip):
    st = {"1": "DISCARD"}
    assert autoclick._sync_from_cache(_chat_item(ts=ts), _CACHED, st, {"1"}, _NOW) is skip


@pytest.mark.parametrize("status", ["DISCARD", "DISCARD_BY_EMPLOYER", "HIRED"])
def test_sync_skips_all_terminal_states(status):
    # единый словарь статусов (chat.TERMINAL_STATES): DISCARD_BY_EMPLOYER тоже терминал
    old = _chat_item(ts="2026-07-01T10:00:00+03:00")
    assert autoclick._sync_from_cache(old, _CACHED, {"1": status}, {"1"}, _NOW) is True


@pytest.mark.parametrize("status", ["RESPONSE", "INTERVIEW", "INVITATION", None])
def test_sync_never_skips_non_terminal_status(status):
    # нетерминальный статус может флипнуться МОЛЧА (отказ без письма) -> качаем всегда
    st = {"1": status} if status else {}
    old = _chat_item(ts="2026-07-01T10:00:00+03:00")
    assert autoclick._sync_from_cache(old, _CACHED, st, {"1"}, _NOW) is False


def test_sync_never_skips_empty_cached_messages():
    # пустая кешевая переписка (артефакт неудачного фетча) не должна замораживаться навсегда
    old = _chat_item(ts="2026-07-01T10:00:00+03:00")
    st = {"1": "DISCARD"}
    assert autoclick._sync_from_cache(old, {"1": {"messages": []}}, st, {"1"}, _NOW) is False


def test_sync_never_skips_chat_missing_from_cache_or_journal():
    old = _chat_item(ts="2026-07-01T10:00:00+03:00")
    st = {"1": "DISCARD"}
    assert autoclick._sync_from_cache(old, {}, st, {"1"}, _NOW) is False        # нет в кеше
    assert autoclick._sync_from_cache(old, _CACHED, st, set(), _NOW) is False   # не журналирован
