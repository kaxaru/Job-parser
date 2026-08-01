"""Формы = часть отклика (RFC-003 + авто-submit): origin-гард, resolve (словарь->suggest->LLM),
try_autofill (полнота -> «Откликнуться»; пробел/пусто -> НЕ шлёт), DOM-литерал вместо exec."""
from types import SimpleNamespace

import pytest

from hrwork.application.apply.forms import forms
from hrwork.application.apply.forms.form_read import FieldType, FormField


def _fld(ftype=FieldType.TEXTAREA, options=(), opt_values=(), prompt="Опыт?"):
    return FormField(selector="input[name=\"task_1\"]", name="task_1", prompt=prompt,
                     ftype=ftype, options=options, opt_values=opt_values)


class _RecPage:
    """Мок Playwright-страницы: пишет, какие селекторы кликнули (проверяем submit)."""
    def __init__(self):
        self.clicks: list[str] = []

    def locator(self, sel):
        page = self

        class _Loc:
            @property
            def first(self):
                return self
            def click(self, **_):
                page.clicks.append(sel)
            def check(self):
                pass
            def fill(self, _v):
                pass
            def select_option(self, **_):
                pass

        return _Loc()

    def wait_for_timeout(self, _ms):
        pass


# ── origin-гард ──
@pytest.mark.parametrize("url,ok", [
    ("https://hh.ru/vacancy/1", True),
    ("https://spb.hh.ru/vacancy/1", True),
    ("http://hh.ru/vacancy/1", False),
    ("https://evil.com/hh.ru", False),
    ("https://hh.ru.evil.com/x", False),
    ("", False),
])
def test_is_hh_origin_guard(url, ok):
    assert forms._is_hh(url) is ok


# ── _resolve: словарь -> suggest -> LLM -> (None,None) ──
def test_resolve_dict_first(monkeypatch):
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: ("Нет", None))
    assert forms._resolve(_fld(FieldType.RADIO, ("Да", "Нет")), "ctx") == ("Нет", None)


def test_resolve_grounded_text_via_suggest(monkeypatch):
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: None)
    monkeypatch.setattr(forms.chat_answer, "suggest", lambda q, **k: {"text": "Да, есть опыт"})
    assert forms._resolve(_fld(FieldType.TEXTAREA), "ctx") == ("Да, есть опыт", None)


def test_resolve_llm_fallback(monkeypatch):
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: None)
    monkeypatch.setattr(forms.chat_answer, "suggest", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_field", lambda *a, **k: "LLM-ответ")
    assert forms._resolve(_fld(FieldType.TEXTAREA), "ctx") == ("LLM-ответ", None)


def test_resolve_all_decline_is_gap(monkeypatch):
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: None)
    monkeypatch.setattr(forms.chat_answer, "suggest", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_field", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_quiz", lambda *a, **k: None)
    assert forms._resolve(_fld(FieldType.RADIO, ("Да", "Нет")), "ctx") == (None, None)


def test_resolve_quiz_fallback_for_options(monkeypatch):
    # ролевой квиз: грунт-ответ DECLINE -> экспертный answer_quiz выбирает опцию
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: None)
    monkeypatch.setattr(forms.form_fill, "answer_field", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_quiz", lambda p, o, c: "Регресс")
    f = _fld(FieldType.RADIO, ("Регресс", "Смоук"), ("a", "b"), prompt="QA: следующий шаг?")
    assert forms._resolve(f, "ctx") == ("Регресс", None)


def test_resolve_without_context_skips_quiz(monkeypatch):
    # выключенный FORMS_LLM доходит сюда пустым resume_ctx (dry-превью): провайдера не трогаем.
    # answer_quiz контекст не проверяет сам — гейт держит вызывающий
    called = []
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: None)
    monkeypatch.setattr(forms.chat_answer, "suggest", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_field", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_quiz", lambda *a, **k: called.append(1))
    assert forms._resolve(_fld(FieldType.RADIO, ("Да", "Нет")), "") == (None, None)
    assert called == []


def test_resolve_no_quiz_for_open_text(monkeypatch):
    # у открытого поля (без вариантов) квиз-fallback не зовём
    called = []
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda p, o: None)
    monkeypatch.setattr(forms.chat_answer, "suggest", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_field", lambda *a, **k: None)
    monkeypatch.setattr(forms.form_fill, "answer_quiz", lambda *a, **k: called.append(1))
    assert forms._resolve(_fld(FieldType.TEXTAREA), "ctx") == (None, None)
    assert called == []


# ── зарплата резолвится КОДОМ и НИКОГДА не уходит в LLM (приватность) ──
def _ban_llm(monkeypatch, sink):
    monkeypatch.setattr(forms.form_fill, "match_answer", lambda *a, **k: sink.append("dict"))
    monkeypatch.setattr(forms.chat_answer, "suggest", lambda *a, **k: sink.append("suggest"))
    monkeypatch.setattr(forms.form_fill, "answer_field", lambda *a, **k: sink.append("llm"))


def test_resolve_salary_uses_code_not_llm(monkeypatch):
    sink = []
    _ban_llm(monkeypatch, sink)
    f = _fld(FieldType.RADIO, ("100к-150к", "150к-200к"), ("a", "b"),
             prompt="Какие у вас ожидания по зарплате?")
    assert forms._resolve(f, "ctx", sal_target=150000) == ("150к-200к", None)
    assert sink == []                                        # ни dict, ни suggest, ни LLM


def test_resolve_salary_no_target_is_gap_not_llm(monkeypatch):
    sink = []
    _ban_llm(monkeypatch, sink)
    f = _fld(FieldType.RADIO, ("100к-150к",), ("a",), prompt="Ваши зарплатные ожидания?")
    assert forms._resolve(f, "ctx", sal_target=None) == (None, None)   # пробел, но
    assert sink == []                                                  # в LLM не пошли


def test_resolve_freetext_salary_writes_amount(monkeypatch):
    # свободный ввод зарплаты (без вариантов) -> «от N руб.» кодом, НЕ через LLM
    sink = []
    _ban_llm(monkeypatch, sink)
    f = _fld(FieldType.TEXTAREA, prompt="На какую вилку заработной платы ориентируешься?")
    assert forms._resolve(f, "ctx", sal_target=150000) == ("от 150 000 руб.", None)
    assert sink == []


# ── письмо: textarea спрятан за тоглом «Добавить» (инцидент 134804227, 2026-07-22:
# _fill_cover молча промахивался — поля нет в DOM, пока тогл не кликнут) ──
class _LetterPage(_RecPage):
    """До клика по _LETTER_TOGGLE поле _LETTER отсутствует; после — появляется."""
    def __init__(self, toggled=False):
        super().__init__()
        self.toggled = toggled
        self.filled: list[tuple[str, str]] = []

    def locator(self, sel):
        page = self

        class _Loc:
            @property
            def first(self):
                return self
            def count(self):
                return int(page.toggled) if sel == forms._LETTER else 1
            def click(self, **_):
                page.clicks.append(sel)
                if sel == forms._LETTER_TOGGLE:
                    page.toggled = True
            def fill(self, v):
                page.filled.append((sel, v))

        return _Loc()


def test_fill_cover_opens_toggle_then_fills(monkeypatch):
    monkeypatch.setattr(forms, "_load_vac_ctx", lambda: {})
    monkeypatch.setattr(forms.cover, "build_cover", lambda cand, mode: "текст письма")
    page = _LetterPage()
    forms._fill_cover(page, "1", {"name": "X"}, "template")
    assert forms._LETTER_TOGGLE in page.clicks
    assert (forms._LETTER, "текст письма") in page.filled


def test_fill_cover_visible_field_needs_no_toggle(monkeypatch):
    monkeypatch.setattr(forms, "_load_vac_ctx", lambda: {})
    monkeypatch.setattr(forms.cover, "build_cover", lambda cand, mode: "текст письма")
    page = _LetterPage(toggled=True)
    forms._fill_cover(page, "1", {"name": "X"}, "template")
    assert forms._LETTER_TOGGLE not in page.clicks
    assert (forms._LETTER, "текст письма") in page.filled


# ── try_autofill: полнота -> submit; пробел/пусто -> НЕ шлёт ──
def _stub_ctx(monkeypatch):
    monkeypatch.setattr(forms.form_fill, "build_resume_ctx", lambda: "ctx")
    monkeypatch.setattr(forms, "_fill_cover", lambda *a, **k: None)


def test_try_autofill_complete_submits(monkeypatch):
    _stub_ctx(monkeypatch)
    monkeypatch.setattr(forms, "_load_vac_ctx", lambda: {})   # без загрузки реального репо (43k)
    monkeypatch.setattr(forms.form_read, "extract_fields",
                        lambda page: [_fld(FieldType.RADIO, ("Да", "Нет"), ("y", "n"))])
    monkeypatch.setattr(forms, "_resolve", lambda f, ctx, sal=None, vac="": ("Нет", None))
    monkeypatch.setattr(forms, "_submitted", lambda page: True)   # HH подтвердил отклик
    page = _RecPage()
    assert forms.try_autofill(page, SimpleNamespace(id="1", name="X")) is True
    assert forms._SUBMIT in page.clicks                       # «Откликнуться» нажат


def test_try_autofill_unconfirmed_is_not_applied(monkeypatch):
    # клик был, но «Вы откликнулись» НЕ появилось -> False (не метим applied, остаётся в очереди)
    _stub_ctx(monkeypatch)
    monkeypatch.setattr(forms, "_load_vac_ctx", lambda: {})
    monkeypatch.setattr(forms.form_read, "extract_fields",
                        lambda page: [_fld(FieldType.RADIO, ("Да", "Нет"), ("y", "n"))])
    monkeypatch.setattr(forms, "_resolve", lambda f, ctx, sal=None, vac="": ("Нет", None))
    monkeypatch.setattr(forms, "_submitted", lambda page: False)  # HH НЕ подтвердил
    assert forms.try_autofill(_RecPage(), SimpleNamespace(id="1", name="X")) is False


def test_try_autofill_polls_for_late_confirmation(monkeypatch):
    # инцидент 134804227 (2026-07-22): отклик УШЁЛ, но подтверждение появилось позже 2с ->
    # прогон счёл «не отправлено». Теперь поллинг: подтверждение с 3-й попытки = успех.
    _stub_ctx(monkeypatch)
    monkeypatch.setattr(forms, "_load_vac_ctx", lambda: {})
    monkeypatch.setattr(forms.form_read, "extract_fields",
                        lambda page: [_fld(FieldType.RADIO, ("Да", "Нет"), ("y", "n"))])
    monkeypatch.setattr(forms, "_resolve", lambda f, ctx, sal=None, vac="": ("Нет", None))
    polls = iter([False, False, True])
    monkeypatch.setattr(forms, "_submitted", lambda page: next(polls))
    assert forms.try_autofill(_RecPage(), SimpleNamespace(id="1", name="X")) is True


def test_try_autofill_gap_never_submits(monkeypatch):
    _stub_ctx(monkeypatch)
    monkeypatch.setattr(forms, "_load_vac_ctx", lambda: {})
    monkeypatch.setattr(forms.form_read, "extract_fields", lambda page: [_fld(FieldType.TEXTAREA)])
    monkeypatch.setattr(forms, "_resolve", lambda f, ctx, sal=None, vac="": (None, None))
    page = _RecPage()
    assert forms.try_autofill(page, SimpleNamespace(id="1", name="X")) is False
    assert forms._SUBMIT not in page.clicks                   # пробел -> вакансия в очередь, НЕ шлём


def test_try_autofill_empty_extraction_never_submits(monkeypatch):
    _stub_ctx(monkeypatch)
    monkeypatch.setattr(forms.form_read, "extract_fields", lambda page: [])
    page = _RecPage()
    assert forms.try_autofill(page, SimpleNamespace(id="1", name="X")) is False
    assert page.clicks == []                                  # непонятную форму не трогаем


# ── sweep: пропускает уже закешированные формы (кеш -> не гоняем браузер повторно) ──
def test_sweep_skips_cached_no_browser(monkeypatch):
    monkeypatch.setattr(forms.store, "forms",
                        lambda: {"111": {"url": "https://hh.ru/vacancy/111", "name": "X"}})
    monkeypatch.setattr(forms.store, "cached_form_ids", lambda: {"111"})
    # всё в кеше -> todo пусто -> возврат до импорта playwright (браузер не поднимается)
    r = forms.sweep()
    assert r == {"queue": 1, "swept": 0, "cached": 1}


# ── FormSweepStatus: VO вместо магических строк "ok"/"empty"/"error" (аудит 2026-07-22) ──
@pytest.mark.parametrize("code, dead", [("ok", False), ("empty", True), ("error", True)])
def test_sweep_status_dead_rule(code, dead):
    assert forms.FormSweepStatus.from_code(code).is_dead is dead


@pytest.mark.parametrize("code", [None, "", "weird"])
def test_sweep_status_unknown_code_is_none(code):
    # мягкий контракт: кеш — внешние данные, мусор -> None, не исключение
    assert forms.FormSweepStatus.from_code(code) is None


# ── clean_queue: убрать мёртвые + уже-откликнутые из форм-очереди (без браузера) ──
def test_clean_queue_removes_dead_and_applied(monkeypatch):
    removed = []
    monkeypatch.setattr(forms.store, "forms",
                        lambda: {"1": {}, "2": {}, "3": {}, "4": {}})
    monkeypatch.setattr(forms.store, "form_cache", lambda: {
        "1": {"status": "ok"}, "2": {"status": "empty"},      # 2 — мёртвая
        "3": {"status": "error"}, "4": {"status": "ok"}})     # 3 — мёртвая
    monkeypatch.setattr(forms.store, "applied_ids", lambda: {"4"})   # 4 — уже откликались
    monkeypatch.setattr(forms.store, "marks", lambda: {})
    monkeypatch.setattr(forms.store, "remove_form", lambda v: removed.append(v))
    r = forms.clean_queue()
    assert set(removed) == {"2", "3", "4"}                    # мёртвые (2,3) + откликнутая (4)
    assert r == {"removed": 3, "dead": 2, "applied": 1, "left": 1}   # осталась только живая «1»


def test_clean_queue_removes_user_rejected(monkeypatch):
    # аудит 2026-07-22: гард сверял marks с несуществующим "discard" (словарь marks —
    # applied|rejected), и вакансия с ручным «Отказ» оставалась кандидатом на авто-отклик
    removed = []
    monkeypatch.setattr(forms.store, "forms", lambda: {"1": {}, "2": {}})
    monkeypatch.setattr(forms.store, "form_cache",
                        lambda: {"1": {"status": "ok"}, "2": {"status": "ok"}})
    monkeypatch.setattr(forms.store, "applied_ids", lambda: set())
    monkeypatch.setattr(forms.store, "marks", lambda: {"2": "rejected"})
    monkeypatch.setattr(forms.store, "remove_form", lambda v: removed.append(v))
    assert forms.clean_queue()["left"] == 1 and removed == ["2"]


def test_clean_queue_keeps_live_unapplied(monkeypatch):
    removed = []
    monkeypatch.setattr(forms.store, "forms", lambda: {"1": {}})
    monkeypatch.setattr(forms.store, "form_cache", lambda: {"1": {"status": "ok"}})
    monkeypatch.setattr(forms.store, "applied_ids", lambda: set())
    monkeypatch.setattr(forms.store, "marks", lambda: {})
    monkeypatch.setattr(forms.store, "remove_form", lambda v: removed.append(v))
    assert forms.clean_queue()["left"] == 1 and removed == []


# ── _fill_field: DOM-литерал, никогда submit; «Свой вариант» -> textarea ──
def test_fill_field_uses_dom_literal_not_submit():
    calls = []

    class _Loc:
        @property
        def first(self):
            return self
        def fill(self, v):
            calls.append(("fill", v))

    class _Page:
        def locator(self, sel):
            return _Loc()

    forms._fill_field(_Page(), _fld(), "текст")
    assert calls == [("fill", "текст")] and not any(c[0] == "click" for c in calls)


def test_fill_field_own_variant_fills_paired_textarea():
    calls = []

    class _Loc:
        @property
        def first(self):
            return self
        def check(self):
            calls.append("check")
        def fill(self, v):
            calls.append(("fill", v))

    class _Page:
        def locator(self, sel):
            calls.append(("loc", sel))
            return _Loc()

    f = _fld(FieldType.RADIO, options=("Свой вариант",), opt_values=("open",))
    forms._fill_field(_Page(), f, "Свой вариант", own="Закончил")
    assert "check" in calls and ("fill", "Закончил") in calls
    assert ("loc", 'textarea[name="task_1_text"]') in calls    # свой-вариант в парный textarea


# ── учёт отправки: дренаж очереди — такой же реальный отклик, как крон-путь ──
# ДЕФЕКТ 28.07: run() звал только remove_form+mark_applied. Суточная квота и журнал его не
# видели: счётчик показывал 35 при реально отправленных ~103, поэтому HH_DAILY_APPLY_CAP не
# сработал и прогон упёрся в лимит HH. В журнал такие отклики попадали лишь позже — синком
# из чатов и с чужим каналом `hh`, как будто человек откликался руками.
def _stub_browser(monkeypatch, autofill_result: bool):
    import contextlib as _ctx

    from hrwork.application.apply import autoclick

    monkeypatch.setattr(forms, "FORMS_ENABLED", True)
    monkeypatch.setattr(forms.time, "sleep", lambda *_a: None)      # FORM_PAUSE не ждём
    monkeypatch.setattr(autoclick, "_single_instance", _ctx.nullcontext)
    monkeypatch.setattr(autoclick, "_launch", lambda *a, **k: object())
    monkeypatch.setattr(autoclick, "_page", lambda *a, **k: _RunPage())
    monkeypatch.setattr(autoclick, "_logged_in", lambda *_a: True)
    monkeypatch.setattr(autoclick, "_goto", lambda *_a: True)
    monkeypatch.setattr(autoclick, "is_captcha", lambda *_a: False)
    monkeypatch.setattr(forms, "_open_form", lambda *_a: None)
    monkeypatch.setattr(forms, "try_autofill", lambda *a, **k: autofill_result)
    monkeypatch.setattr(forms, "sync_playwright", _ctx.nullcontext, raising=False)


class _RunPage:
    def set_default_navigation_timeout(self, *_a):
        pass

    def set_default_timeout(self, *_a):
        pass

    def wait_for_timeout(self, *_a):
        pass


@pytest.fixture
def drain_env(monkeypatch):
    """Очередь из одной вакансии + перехват всех записей учёта."""
    seen: dict = {"quota": 0, "journal": [], "employers": [], "marked": [], "removed": []}
    monkeypatch.setattr(forms.store, "forms",
                        lambda: {"111": {"name": "Backend разработчик",
                                         "url": "https://hh.ru/vacancy/111"}})
    # Контекст вакансий — заглушкой: в очереди лежат только {name, url, ts}, работодателя
    # дренаж берёт отсюда. Без подмены _load_vac_ctx() полез бы в реальный vacancies_raw.json.
    monkeypatch.setattr(forms, "_VAC_CTX",
                        {"111": ("Константинов Семен Павлович", "", "Backend разработчик", None)})
    monkeypatch.setattr(forms.store, "applied_ids", set)
    monkeypatch.setattr(forms.store, "marks", dict)
    monkeypatch.setattr(forms.store, "remove_form", lambda v: seen["removed"].append(v))
    monkeypatch.setattr(forms.store, "mark_applied", lambda v: seen["marked"].append(v))
    monkeypatch.setattr(forms.store, "bump_quota",
                        lambda n: seen.__setitem__("quota", seen["quota"] + n))
    def _log(vid, name, url, via, **k):
        seen["journal"].append((vid, via.code))
        seen["employers"].append(k.get("employer", ""))
    monkeypatch.setattr(forms.store, "log_applied", _log)
    return seen


def test_sent_form_reply_counts_toward_daily_quota(monkeypatch, drain_env):
    import sys
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        type(sys)("playwright.sync_api"))
    import contextlib as _ctx
    sys.modules["playwright.sync_api"].sync_playwright = _ctx.nullcontext
    _stub_browser(monkeypatch, autofill_result=True)
    assert forms.run() == {"forms": 1, "submitted": 1}
    assert drain_env["quota"] == 1
    assert drain_env["journal"] == [("111", "cron")]
    assert drain_env["marked"] == ["111"]
    assert drain_env["removed"] == ["111"]


def test_drained_form_writes_employer_to_journal(monkeypatch, drain_env):
    # Регрессия 01.08.2026: дренаж форм писал журнал без работодателя (в очереди его нет),
    # и «призрак» выпавшей из выдачи вакансии не находился поиском по компании.
    import sys
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        type(sys)("playwright.sync_api"))
    import contextlib as _ctx
    sys.modules["playwright.sync_api"].sync_playwright = _ctx.nullcontext
    _stub_browser(monkeypatch, autofill_result=True)
    forms.run()
    assert drain_env["employers"] == ["Константинов Семен Павлович"]


def test_unsent_form_leaves_quota_untouched(monkeypatch, drain_env):
    # пробел/неподтверждённая отправка -> вакансия остаётся в очереди, счётчики не трогаем
    import sys
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        type(sys)("playwright.sync_api"))
    import contextlib as _ctx
    sys.modules["playwright.sync_api"].sync_playwright = _ctx.nullcontext
    _stub_browser(monkeypatch, autofill_result=False)
    assert forms.run() == {"forms": 1, "submitted": 0}
    assert drain_env["quota"] == 0
    assert drain_env["journal"] == []
    assert drain_env["removed"] == []


# ── полнота ЗАПОЛНЕНИЯ, а не только резолва ──
# ДЕФЕКТ: результат _fill_field выбрасывался, и submit жался даже когда поле не вписалось
# (селектор разъехался / вариант не совпал / поле скрыто). Инвариант «шлём ТОЛЬКО при
# полноте» должен покрывать оба этапа: ответ найден И вписан в форму.
def _autofill_env(monkeypatch, fill_ok):
    monkeypatch.setattr(forms.form_read, "extract_fields",
                        lambda _p: [_fld(FieldType.RADIO, ("Да", "Нет"), ("1", "0"))])
    monkeypatch.setattr(forms.form_fill, "build_resume_ctx", lambda: "ctx")
    monkeypatch.setattr(forms, "_vacancy_ctx", lambda _v: ("", "", ""))
    monkeypatch.setattr(forms, "_vacancy_floor", lambda _v: None)
    monkeypatch.setattr(forms, "_resolve", lambda *a, **k: ("Да", None))
    monkeypatch.setattr(forms, "_fill_field", lambda *a, **k: fill_ok)
    monkeypatch.setattr(forms, "_fill_cover", lambda *a, **k: None)
    monkeypatch.setattr(forms, "_wait_submitted", lambda *a, **k: True)


def test_unfilled_field_blocks_submit(monkeypatch):
    page = _RecPage()
    _autofill_env(monkeypatch, fill_ok=False)
    assert forms.try_autofill(page, SimpleNamespace(id="1", name="X")) is False
    assert page.clicks == []                      # submit НЕ нажат


def test_filled_field_allows_submit(monkeypatch):
    page = _RecPage()
    _autofill_env(monkeypatch, fill_ok=True)
    assert forms.try_autofill(page, SimpleNamespace(id="1", name="X")) is True
    assert page.clicks == [forms._SUBMIT]


# ── метки CRM: тип вместо магических строк ──
def test_settled_marks_are_applied_and_rejected():
    from hrwork.application.apply.outcome import SETTLED_MARKS, VacancyMark
    assert VacancyMark.APPLIED.code == "applied"
    assert VacancyMark.REJECTED.code == "rejected"
    assert sorted(SETTLED_MARKS) == ["applied", "rejected"]
