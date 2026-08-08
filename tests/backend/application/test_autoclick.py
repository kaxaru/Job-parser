"""Тесты отбора кандидатов, дневной квоты и single-instance lock (без браузера).

Отбор считается на ДЕФОЛТАХ проекта, а не на профиле запускающего: `candidates.py`
компилирует чёрные списки и собирает `APPLY_CORE`/`APPLY_EXPS`/`APPLY_OFFICE_CITIES` один
раз на импорте, из `resume_profile.json`. Владелец, снявший правило документированной
ручкой (`"blacklists": {"qa": []}`) или изменивший города тира-3, получал бы либо десятки
красных тестов, либо — хуже — зелёные тесты, проверяющие его личные предпочтения
(аудит 08.08.2026, находка 57). Дефолты возвращает фикстура `apply_defaults`;
доказательство механизма — tests/backend/test_profile_isolation.py.
"""
import datetime
import json
import os
import subprocess
import sys

import pytest

from hrwork.application.apply import autoclick
from hrwork.application.apply.autoclick import pick_candidates
from hrwork.application.apply.candidates import ApplyTier, OutOfScope, out_of_scope
from hrwork.application.apply.runtime import lock, quota
from hrwork.application.apply.runtime.quota import applied_today
from hrwork.infrastructure.storage import JsonVacancyRepository

pytestmark = pytest.mark.usefixtures("apply_defaults")


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
])
def test_keeps_dev_roles_in_same_domain(name):
    assert len(pick_candidates([_raw(name=name)], marks={}, limit=10)) == 1


# ── Целевая специализация: backend + data/LLM engineering (задана 07.08.2026) ──
# Прежнее требование было ОБРАТНЫМ — айтишные аналитики проходили (APPLY_ANALYST_OK).
# Отменено сознательно, поэтому тесты на пропуск аналитиков заменены на тесты отсева.
@pytest.mark.parametrize("name", [
    "Аналитик данных", "Data Analyst", "Дата-аналитик", "Аналитик данных (Junior)",
    "Системный аналитик", "Бизнес-аналитик", "Product Analyst (AI tools)",
    "BI-аналитик", "Финансовый аналитик", "Аналитик-экономист",
])
def test_all_analysts_are_out_of_scope(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


@pytest.mark.parametrize("name", [
    "QA Engineer", "QA Automation Engineer (Python)", "Middle AQA Python инженер",
    "SDET", "Тестировщик", "Инженер по нагрузочному тестированию",
    "Автотестировщик Python", "Test Engineer", "Quality Assurance Specialist",
])
def test_all_qa_is_out_of_scope(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


# QA на LLM/DWH-продукте — всё ещё QA: исключение для гибридов на него НЕ распространяется
@pytest.mark.parametrize("name", [
    "QA Engineer (LLM-платформа)",
    "QA Engineer (DWH / ETL)",
    "Тестировщик (LLM, ML)",
])
def test_qa_is_out_of_scope_even_on_target_products(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


def test_data_analyst_on_a_data_platform_is_still_an_analyst():
    # «платформа данных» описывает ПРОДУКТ работодателя, а не инженерную роль
    name = "Data - аналитик (Платформа данных для финансовой отчетности)"
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


@pytest.mark.parametrize("name", [
    "ML инженер (Python)", "Python / MLOps engineer", "Python ML-разработчик",
    "Machine Learning Engineer", "Data Scientist", "Стажёр-дата-сайентист",
    "Инженер машинного обучения", "NLP Engineer", "Computer Vision Engineer",
])
def test_ml_and_ds_are_out_of_scope(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


# Инженерный маркер перевешивает «аналитика»/ML рядом с ним — это целевые вакансии.
@pytest.mark.parametrize("name", [
    "Data Engineer/Data Analyst",
    "Data engineer+analyst (DWH)",
    "Системный аналитик DWH/BI",
    "Data Scientist (NLP / LLM) в команду СберБизнес",
    "LLM Engineer",
    "AI-разработчик / LLM Engineer",
    "Инженер данных (ETL)",
])
def test_target_engineering_survives_the_blacklists(name):
    assert len(pick_candidates([_raw(name=name)], marks={}, limit=10)) == 1


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
    assert [c.tier for c in out] == [ApplyTier.OFFICE]


def test_tier2_wide_stack_remote():
    # удалёнка, без Python/FastAPI, но с широким стеком (Django) -> WIDE
    raw = _raw(name="Backend разработчик", req="Django, PostgreSQL, удалённая работа")
    out = pick_candidates([raw], marks={}, limit=10)
    assert [c.tier for c in out] == [ApplyTier.WIDE]


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
    "Senior Python разработчик", "Ведущий backend-разработчик Python",
    "Старший Python-разработчик", "Python Team Lead", "Python Backend (Lead)",
    "Тимлид Python",
])
def test_blacklist_senior_grades(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


# ── Руководящие должности: всё выше lead (задано 07.08.2026) ──
@pytest.mark.parametrize("name", [
    "Руководитель группы разработки",
    "Руководитель отдела транспортной сети и инфраструктуры",
    "Начальник управления разработки",
    "Технический директор / CTO (Chief Technology Officer)",
    "Head of Engineering",
    "Director of Platform",
    "VP of Engineering",
    "Delivery Manager / Presale Manager (Big Data)",
    "Engineering Manager",
    "Solution Architect",
    "Архитектор решений",
    "Заместитель руководителя департамента разработки ПО",
])
def test_management_roles_are_out_of_scope(name):
    assert pick_candidates([_raw(name=name)], marks={}, limit=10) == []


# Управленческое слово, стоящее ПОСЛЕ инженерного, роли не задаёт: реальный случай из пула —
# «директор» в названии продукта, а вакансия разработчика.
@pytest.mark.parametrize("name", [
    "Python-разработчик (AI-агент Операционный директор)",
    "Backend-разработчик в команду директора по данным",
    "Python developer, platform architect team",
])
def test_engineer_noun_before_management_word_keeps_the_vacancy(name):
    assert len(pick_candidates([_raw(name=name)], marks={}, limit=10)) == 1


# ── out_of_scope: единый предикат отбора по тайтлу (его же зовёт forms.clean_queue) ──
# Причина — VO (OutOfScope), а не строка: она уходит в логи и сводки, набор значений конечен.
@pytest.mark.parametrize("name, expected", [
    ("Senior Python разработчик",          OutOfScope.SENIOR),
    ("Руководитель группы разработки",     OutOfScope.MANAGEMENT),
    ("Риск-аналитик (проект ПВР)",         OutOfScope.NON_ENGINEERING),
    ("QA Automation Engineer (Python)",    OutOfScope.QA),
    ("Аналитик данных",                    OutOfScope.ANALYST),
    ("Data Scientist",                     OutOfScope.ML),
    ("DevOps-инженер",                     OutOfScope.DEVOPS),
    ("SRE/DevOps Engineer",                OutOfScope.DEVOPS),
    ("Системный администратор",            OutOfScope.DEVOPS),
    ("Java разработчик",                   OutOfScope.OTHER_LANG),
    ("Python-разработчик",                 None),
    ("Data Engineer/Data Analyst",         None),   # инженерный маркер перевешивает
    ("ML-инженер (LLM / RAG)",             None),
])
def test_out_of_scope_names_the_reason(name, expected):
    assert out_of_scope(name) is expected


def test_out_of_scope_order_qa_wins_over_the_engineering_exemption():
    # «QA Engineer (LLM-платформа)» — тестирование на целевом продукте, а не целевая роль
    assert out_of_scope("QA Engineer (LLM-платформа)") is OutOfScope.QA


# Код в тайтле снимает devops-запрет: интересует разработка, а не эксплуатация, но эти
# четыре гибрида из живого пула — именно разработка.
@pytest.mark.parametrize("name", [
    "LLM Platform Engineer",
    "Инженер-программист по безопасной разработке / Middle DevSecOps",
    "Разработчик в инфраструктуру симулятора",
    "Python-разработчик (DevOps-практики)",
])
def test_code_marker_beats_the_devops_blacklist(name):
    assert out_of_scope(name) is None


def test_out_of_scope_labels_are_stable():
    # подпись уходит в лог сводки чистки очереди — фиксируем литералом
    assert OutOfScope.MANAGEMENT.label == "руководящая"
    assert OutOfScope.QA.label == "QA"


def test_blacklist_does_not_hit_html_xml():
    # \bml\b не должен цеплять HTML/XML в тайтле
    ok = _raw(name="Python разработчик (HTML/XML парсеры)")
    assert len(pick_candidates([ok], marks={}, limit=10)) == 1


@pytest.mark.parametrize("title", [
    "Java разработчик",
    "C# Backend Developer",
    "PHP-программист",
    "Golang разработчик",
    "1С-программист",
])
def test_skips_other_language_in_title_even_if_python_in_body(title):
    # Java-вакансия с Python «как плюс» в требованиях — Python попадёт в techs и пройдёт
    # core-фильтр, но другой язык в ТАЙТЛЕ должен отсеять («ищу только python»).
    raw = _raw(name=title, req="Java, Spring, Python как плюс, удалённая работа")
    assert pick_candidates([raw], marks={}, limit=10) == []


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
_QUOTA_DAY = "2026-08-08"      # «сегодня» квоты, фиксированное -> прогон не зависит от часов


@pytest.fixture
def tmp_quota(tmp_path, monkeypatch):
    """Файл счётчика во временном каталоге + ЗАМОРОЖЕННЫЕ сутки.

    ФЛАК (аудит 08.08.2026): счётчик спрашивает `date.today()` на КАЖДОМ обращении, поэтому
    `bump_quota(3); bump_quota(2)` в 23:59:59 попадали в разные сутки и второй вызов начинал
    счёт заново. Дата замораживается на уровне `quota._today` — публичного шва у модуля нет."""
    f = tmp_path / "apply_quota.json"
    monkeypatch.setattr(quota, "QUOTA_FILE", f)
    monkeypatch.setattr(quota, "DATA_DIR", tmp_path)
    monkeypatch.setattr(quota, "_today", lambda: _QUOTA_DAY)
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


# ── Реконсиляция квоты: окно «клик -> учёт» (аудит 08.08.2026) ──────────────────────────
# Между кликом «Откликнуться» и bump_quota есть щель: watchdog сносит дерево, либо HH
# подтверждает отклик на 11-й секунде, когда apply_one уже вернул SKIP. Отклик УШЁЛ, а
# счётчик его не увидел; marks и журнал чинил синк, квоту — никто, и за сутки бот слал cap+N.

def test_reconcile_raises_the_counter_to_the_journal_fact(tmp_quota):
    quota.bump_quota(35)
    assert quota.reconcile_quota(103) == 103
    assert applied_today() == 103


def test_reconcile_never_lowers_the_counter(tmp_quota):
    # журнал догоняет реальность с задержкой; «выравнивание» вниз открыло бы путь к cap+N
    quota.bump_quota(103)
    assert quota.reconcile_quota(35) == 103
    assert applied_today() == 103


def test_reconcile_is_idempotent(tmp_quota):
    quota.bump_quota(10)
    assert quota.reconcile_quota(12) == 12
    assert quota.reconcile_quota(12) == 12
    assert applied_today() == 12


def test_reconcile_ignores_yesterdays_counter(tmp_quota):
    tmp_quota.write_text(json.dumps({"date": "2000-01-01", "count": 199}), encoding="utf-8")
    assert quota.reconcile_quota(4) == 4        # вчерашние 199 = 0 сегодня -> поднимаем до факта


def test_journal_counts_todays_applies_deduped_by_id(monkeypatch):
    # Сутки задаются ЯВНО (параметр `today` функции): без этого журнал строился по
    # `date.today()` теста, а считался по `date.today()` реализации — на границе суток это
    # два разных дня и ноль вместо двух (аудит 08.08.2026).
    rows = [
        {"id": "1", "ts": "2026-08-08T10:00:00+03:00"},
        {"id": "1", "ts": "2026-08-08T11:00:00+03:00"},   # тот же отклик вторым каналом
        {"id": "2", "ts": "2026-08-08T12:00:00+03:00"},
        {"id": "3", "ts": "2000-01-01T09:00:00+03:00"},   # прошлый день
        {"id": "4", "ts": ""},                            # без даты
    ]
    monkeypatch.setattr(autoclick.store, "applied_log", lambda: rows)
    assert autoclick._journal_applied_today("2026-08-08") == 2


def test_quota_is_raised_when_the_click_never_reached_the_counter(monkeypatch):
    got = []
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 35)
    monkeypatch.setattr(autoclick, "_journal_applied_today", lambda: 103)
    monkeypatch.setattr(autoclick.store, "reconcile_quota", lambda n: got.append(n) or n)
    assert autoclick._reconciled_today() == 103
    assert got == [103]


def test_quota_is_left_alone_when_the_journal_agrees(monkeypatch):
    def forbidden(n):
        raise AssertionError("квоту трогать нельзя, расхождения нет")
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 40)
    monkeypatch.setattr(autoclick, "_journal_applied_today", lambda: 40)
    monkeypatch.setattr(autoclick.store, "reconcile_quota", forbidden)
    assert autoclick._reconciled_today() == 40


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


# ── Захват атомарен (аудит 08.08.2026): check-then-write пускал два Chromium на профиль ──
# Крон-слот и ручной `hh.py forms` могли пройти проверку живости в одно окно и оба «взять»
# lock. Окно узаконил watchdog-путь: _kill_own_tree отдаёт lock ДО выстрела, пока Chromium
# ещё умирает. O_EXCL отдаёт файл ровно одному претенденту — это гарантия ФС.

def test_lock_file_is_never_overwritten_by_a_second_acquirer(tmp_lock):
    tmp_lock.write_text(json.dumps({"pid": 424242, "ts": lock.time.time()}), encoding="utf-8")
    assert lock._try_acquire() is False
    assert json.loads(tmp_lock.read_text())["pid"] == 424242     # чужая запись цела


def test_only_one_of_concurrent_acquirers_gets_the_lock(tmp_lock):
    import threading
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        results.append(lock._try_acquire())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1              # ровно один хозяин профиля
    assert results.count(False) == 7


def test_acquired_lock_carries_our_pid(tmp_lock):
    assert lock._try_acquire() is True
    assert json.loads(tmp_lock.read_text())["pid"] == os.getpid()


def test_stale_drop_keeps_a_lock_that_became_live(tmp_lock, monkeypatch):
    # пока мы решались снять протухший файл, конкурент положил свой ЖИВОЙ — снести его
    # значило бы пустить второй Chromium на persistent-профиль
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    tmp_lock.write_text(json.dumps({"pid": 424242, "ts": lock.time.time()}), encoding="utf-8")
    assert lock._drop_stale() is False
    assert tmp_lock.exists()


def test_stale_drop_removes_an_orphaned_lock(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: False)
    tmp_lock.write_text(json.dumps({"pid": 424242, "ts": lock.time.time()}), encoding="utf-8")
    assert lock._drop_stale() is True
    assert not tmp_lock.exists()


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
        # Именно НАШ pid, а не «любой чужой»: lock с pid=0 или мусором `release_if_mine`
        # уже не снимет («чужой lock не наш, чтобы снимать»), и осиротевший файл заблокирует
        # крон-слоты откликов на LOCK_TTL — инцидент 25.07.2026 (аудит 09.08.2026).
        assert json.loads(tmp_lock.read_text())["pid"] == os.getpid()


# ── _lock_holder / ретрай ожидания lock (для крона) ──
def test_lock_holder_none_when_absent(tmp_lock):
    assert lock._lock_holder() is None


def test_lock_holder_reports_live_instance(tmp_lock, monkeypatch):
    monkeypatch.setattr(lock, "_pid_alive", lambda pid: True)
    tmp_lock.write_text(json.dumps({"pid": 42, "ts": lock.time.time()}), encoding="utf-8")
    held = lock._lock_holder()
    assert held is not None
    assert held[0] == 42


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
        assert json.loads(tmp_lock.read_text())["pid"] == os.getpid()          # взяли СВОЙ lock


def test_lock_holder_broken_json_recent_is_held(tmp_lock):
    # битый/недописанный lock со свежим mtime -> НЕ «свободен» (иначе второй браузер)
    tmp_lock.write_text("{not json", encoding="utf-8")
    held = lock._lock_holder()
    assert held is not None
    assert held[0] == "?"


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
    # Модульный синглтон возвращает monkeypatch, а не присваивание в конце теста: до
    # 08.08.2026 восстановление стояло ПОСЛЕ ассертов и падение теста оставляло в
    # `autoclick._apply_worker` живой ApplyWorker — следующему тесту доставался чужой воркер.
    monkeypatch.setattr(autoclick, "_apply_worker", None)
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


# ── Watchdog: пороги проверены замером (03.08.2026) ─────────────────────────────────────
# Дедлайна на ОТДЕЛЬНЫЙ Playwright-вызов не существует: sync-API привязан к своему потоку
# (вызов из демон-потока валит драйвер с greenlet.error), а залипший вызов отпускает python
# только со смертью процесса. Watchdog — единственное средство, и порог снижать некуда.

def test_watchdog_threshold_covers_the_longest_healthy_run():
    """Замер 128 прогонов: медиана 27 мин, p90 36, максимум 37.4. Порог обязан быть выше,
    иначе watchdog начнёт резать рабочие прогоны вместо зависших."""
    longest_healthy_s = 38 * 60
    assert longest_healthy_s < autoclick.WATCHDOG_KILL_S


def test_watchdog_fires_before_the_next_cron_slot():
    """Слоты идут каждые 90 минут: зависший прогон обязан умереть до следующего."""
    assert autoclick.WATCHDOG_KILL_S < 90 * 60
    assert autoclick.WATCHDOG_DUMP_S < autoclick.WATCHDOG_KILL_S   # дамп стека ДО сноса


# ── Предохранитель от блокировки HH (02.08.2026) ────────────────────────────────────────
# «Кнопки отклика нет» трактуется как архив/внешний сайт. При мягкой блокировке аккаунта
# страница грузится, React-корень на месте, а кнопки нет — и прогон час молол активные
# вакансии, каждой карточкой подтверждая HH, что перед ним бот. Замер 19 суток лога:
# отношение пропусков к откликам выросло с 0.3 до 36, максимальная серия — 268 подряд.

_SKIP_STREAK_MAX = 50      # дефолт config.APPLY_SKIP_STREAK_MAX (env-ручка того же имени)


@pytest.fixture
def batch_env(monkeypatch):
    """Пул из 200 кандидатов; все браузерные вызовы и запись состояния — заглушками.

    Порог предохранителя прибит к ДЕФОЛТУ: он приходит из переменной окружения, то есть у
    запускающего со своим `.env` был бы другой, а ожидаемое ниже — литерал, а не
    `autoclick.APPLY_SKIP_STREAK_MAX` (иначе тест повторяет реализацию и пройдёт при любом
    её значении, включая порог больше пула)."""
    monkeypatch.setattr(autoclick, "APPLY_SKIP_STREAK_MAX", _SKIP_STREAK_MAX)
    seen = {"applied": [], "quota": 0, "forms": []}
    pool = [autoclick.Candidate(id=str(i), name=f"Вакансия {i}", url=f"https://hh.ru/vacancy/{i}")
            for i in range(200)]
    monkeypatch.setattr(autoclick, "pick_candidates", lambda *a, **k: pool)
    monkeypatch.setattr(autoclick, "vacancy_repository", lambda: type("R", (), {"load": staticmethod(list)})())
    monkeypatch.setattr(autoclick, "skippable_form_ids", lambda *a, **k: set())
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 0)
    monkeypatch.setattr(autoclick.store, "applied_log", list)   # журнал пуст -> квота не правится
    monkeypatch.setattr(autoclick.store, "marks", dict)
    monkeypatch.setattr(autoclick.store, "forms", dict)
    monkeypatch.setattr(autoclick.store, "form_cache", dict)
    monkeypatch.setattr(autoclick.store, "add_form",
                        lambda *a, **k: seen["forms"].append(a[0]) or True)
    monkeypatch.setattr(autoclick.store, "mark_applied", lambda v: seen["applied"].append(v))
    monkeypatch.setattr(autoclick.store, "bump_quota", lambda n: seen.__setitem__("quota", seen["quota"] + n) or seen["quota"])
    monkeypatch.setattr(autoclick.store, "log_applied", lambda *a, **k: None)
    monkeypatch.setattr(autoclick.store, "merge_marks", lambda m: None)
    monkeypatch.setattr(autoclick, "_send_cover_via_chat", lambda *a, **k: True)
    monkeypatch.setattr(autoclick.time, "sleep", lambda s: None)
    return seen


def test_blocked_account_stops_run_instead_of_grinding_the_pool(batch_env, monkeypatch):
    """Все карточки без кнопки -> останов на пороге, а не перемалывание всего пула."""
    tried = []

    def always_skip(page, cand, **kw):
        tried.append(cand.id)
        return autoclick.ApplyOutcome.SKIP

    monkeypatch.setattr(autoclick, "apply_one", always_skip)
    assert autoclick._apply_batch(page=None, apply_limit=20, daily_cap=200) == 0
    assert len(tried) == 50                     # ровно порог (пул 200) — не весь пул


def test_successful_apply_resets_the_skip_streak(batch_env, monkeypatch):
    """Пропуски вперемешку с откликами — норма пула (архив, уже откликались, внешние):
    серия обнуляется, и прогон продолжается до цели по откликам."""
    seq = []

    def alternate(page, cand, **kw):
        # 40 пропусков, затем отклик — и так по кругу: до порога 50 подряд не доходит
        i = int(cand.id)
        out = autoclick.ApplyOutcome.APPLIED if i % 41 == 40 else autoclick.ApplyOutcome.SKIP
        seq.append(out)
        return out

    monkeypatch.setattr(autoclick, "apply_one", alternate)
    applied = autoclick._apply_batch(page=None, apply_limit=3, daily_cap=200)
    assert applied == 3
    assert len(seq) == 123                # 3 отклика × 41 карточка: порог 50 не сработал ни разу


# ── Сводка прогона: анкеты видны наравне с откликами и пропусками (аудит 08.08.2026) ────
# Анкета — не отклик и не пропуск, поэтому в отношении «скип/отклик» её не было вовсе:
# HH массово включает опросники, прогон даёт 3 отклика и 20 анкет, «пропусков 0», отношение
# в норме — и падение темпа не видно неделями. Это ровно класс «21 отклик в день вместо 200».

@pytest.mark.parametrize("applied, skipped, forms, reconciled, level, line", [
    (10, 3, 0, 2, "info",
     "Итог прогона: откликов 10, анкет 0, пропусков 3 (скип/отклик 0.3), уже откликались 2"),
    (3, 0, 20, 0, "warning",
     "Итог прогона: откликов 3, анкет 20, пропусков 0 (скип/отклик 0.0), уже откликались 0"),
    (5, 0, 5, 0, "info",
     "Итог прогона: откликов 5, анкет 5, пропусков 0 (скип/отклик 0.0), уже откликались 0"),
    (5, 0, 10, 0, "warning",
     "Итог прогона: откликов 5, анкет 10, пропусков 0 (скип/отклик 0.0), уже откликались 0"),
    (2, 10, 0, 1, "warning",
     "Итог прогона: откликов 2, анкет 0, пропусков 10 (скип/отклик 5.0), уже откликались 1"),
    (0, 7, 0, 0, "warning",
     "Итог прогона: откликов 0, анкет 0, пропусков 7 (скип/отклик все), уже откликались 0"),
])
def test_run_summary_names_forms_and_flags_degradation(applied, skipped, forms, reconciled,
                                                       level, line):
    assert autoclick._run_summary(applied, skipped, forms, reconciled) == (level, line)


def test_form_outcomes_reach_the_run_summary(batch_env, monkeypatch):
    """21 анкета на 3 отклика: анкеты обязаны доехать до счётчика сводки."""
    calls = []
    monkeypatch.setattr(autoclick, "_run_summary",
                        lambda *a: calls.append(a) or ("info", "сводка"))

    def survey_heavy(page, cand, **kw):
        # каждая восьмая карточка — отклик, остальные семь уходят анкетами
        return (autoclick.ApplyOutcome.APPLIED if int(cand.id) % 8 == 7
                else autoclick.ApplyOutcome.FORM)

    monkeypatch.setattr(autoclick, "apply_one", survey_heavy)
    assert autoclick._apply_batch(page=None, apply_limit=3, daily_cap=200) == 3
    assert calls == [(3, 0, 21, 0)]          # (откликов, пропусков, анкет, уже откликались)


def test_batch_budget_uses_the_reconciled_quota(batch_env, monkeypatch):
    """Отклик, ушедший мимо счётчика, обязан съедать дневной лимит: журнал знает 200 при
    счётчике 0 -> лимит исчерпан, ни одной новой карточки не трогаем."""
    tried = []
    monkeypatch.setattr(autoclick, "_journal_applied_today", lambda: 200)
    monkeypatch.setattr(autoclick.store, "reconcile_quota", lambda n: n)
    monkeypatch.setattr(autoclick, "apply_one",
                        lambda *a, **k: tried.append(1) or autoclick.ApplyOutcome.APPLIED)
    assert autoclick._apply_batch(page=None, apply_limit=10, daily_cap=200) == 0
    assert tried == []


# ─── Стажировки вне отбора, junior — в отборе (задано пользователем 08.08.2026) ──────
# Стажировка ниже целевого грейда: срочный договор, учебная нагрузка, ставка ниже рынка.
# Junior при этом штатная позиция, ради которой отбор и настроен, — правило обязано
# различать их, а не резать «всё, что ниже middle».
@pytest.mark.parametrize("name", [
    "Python Developer Intern",
    "Internship: Backend (Python)",
    "Trainee Software Engineer",
    "Стажёр-разработчик Python",
    "Стажер бэкенд-разработки",
    "Стажировка в команду бэкенда",
    "Практикант-программист",
])
def test_internships_are_out_of_scope(name):
    assert out_of_scope(name) is OutOfScope.INTERNSHIP


@pytest.mark.parametrize("name", [
    "Junior Python Developer",
    "Джуниор бэкенд-разработчик",
    "Python-разработчик (Junior)",
])
def test_junior_stays_in_scope(name):
    assert out_of_scope(name) is None


# `\bintern\b` целым словом: соседние слова с той же основой — обычные вакансии
@pytest.mark.parametrize("name", [
    "Internal Tools Developer (Python)",
    "International Payments Backend Engineer",
])
def test_words_starting_with_intern_are_not_internships(name):
    assert out_of_scope(name) is None


def test_internship_wins_over_qa_in_the_reason(name="Стажёр QA Automation"):
    # порядок таблицы значим: «Стажёр QA» интереснее назвать стажировкой, чем тестированием
    assert out_of_scope(name) is OutOfScope.INTERNSHIP
