"""LLM-заполнение форм (RFC-003). Свойства: выход санитайзится/DECLINE, select строго из опций,
зарплата/гражданство не уходят провайдеру, инъекция уходит лишь инертной строкой. chat_json замокан."""
import ast
import importlib
import pathlib

import pytest

from hrwork.application.apply.forms import form_fill as F
from hrwork.application.apply.forms.form_read import FieldType


def _mock(monkeypatch, ret, seen=None):
    def transport(system, user, **k):
        if seen is not None:
            seen["user"], seen["system"] = user, system
        return ret
    monkeypatch.setattr(F, "chat_json", transport)


# ── _sanitize: TEXTAREA пропускает код, TEXT режет URL, control/фенсы/длина ──
def test_textarea_keeps_code_angle_brackets():
    assert F._sanitize("vector<int> x; a && b", FieldType.TEXTAREA) == "vector<int> x; a && b"


def test_text_strips_url():
    out = F._sanitize("резюме тут http://evil.io/x", FieldType.TEXT)
    assert "http" not in out and "evil" not in out


def test_control_chars_and_fences_stripped():
    assert F._sanitize("```\nответ\x00\x07\n```", FieldType.TEXTAREA) == "ответ"


@pytest.mark.parametrize("raw", ["", "   ", "DECLINE", None])
def test_empty_or_decline_returns_none(raw):
    assert F._sanitize(raw, FieldType.TEXT) is None


def test_over_length_bounded():
    from hrwork.config import FORM_MAX_ANSWER_LEN
    assert len(F._sanitize("x" * (FORM_MAX_ANSWER_LEN + 500), FieldType.TEXTAREA)) == FORM_MAX_ANSWER_LEN


# ── _match_option: select/radio строго из вариантов ──
def test_option_match_normalized():
    assert F._match_option("  да  ", ("Да", "Нет")) == "Да"


def test_option_miss_returns_none():
    assert F._match_option("может быть", ("Да", "Нет")) is None


# ── answer_field: оркестрация + fallback-safe ──
def test_decline_field_returns_none(monkeypatch):
    _mock(monkeypatch, "DECLINE")
    assert F.answer_field("Опыт с Rust?", FieldType.TEXTAREA, (), "стек: Python") is None


def test_no_key_returns_none(monkeypatch):
    _mock(monkeypatch, None)
    assert F.answer_field("вопрос", FieldType.TEXTAREA, (), "ctx") is None


def test_empty_ctx_no_network(monkeypatch):
    called = []
    monkeypatch.setattr(F, "chat_json", lambda *a, **k: called.append(1))
    assert F.answer_field("вопрос", FieldType.TEXTAREA, (), "  ") is None
    assert called == []


def test_select_answer_must_be_in_options(monkeypatch):
    _mock(monkeypatch, "Да")
    assert F.answer_field("Готовы?", FieldType.SELECT, ("Да", "Нет"), "ctx") == "Да"
    _mock(monkeypatch, "Наверное")                       # вне опций -> None
    assert F.answer_field("Готовы?", FieldType.SELECT, ("Да", "Нет"), "ctx") is None


def test_open_text_returned(monkeypatch):
    _mock(monkeypatch, "Пишу на Python.")
    assert F.answer_field("Про опыт?", FieldType.TEXTAREA, (), "стек Python") == "Пишу на Python."


# ── answer_quiz: экспертный ответ на технический квиз (LLM -> номер -> опция) ──
def test_quiz_picks_option_by_number(monkeypatch):
    _mock(monkeypatch, "2")                              # LLM вернул номер варианта
    opts = ("Углубиться в тестирование белым ящиком",
            "Провести целенаправленное регрессионное тестирование затронутых интеграций")
    assert F.answer_quiz("Изменение затронуло смежные системы. Шаг?", opts, "ctx") == opts[1]


def test_quiz_number_in_sentence(monkeypatch):
    _mock(monkeypatch, "Ответ: 3")                       # номер внутри фразы -> парсим
    assert F.answer_quiz("Q?", ("A", "B", "C"), "ctx") == "C"


def test_quiz_index_out_of_range_is_none(monkeypatch):
    _mock(monkeypatch, "9")
    assert F.answer_quiz("Q?", ("A", "B"), "ctx") is None


def test_quiz_declines_personal(monkeypatch):
    _mock(monkeypatch, "DECLINE")
    assert F.answer_quiz("Ваш возраст?", ("18-25", "26-35"), "ctx") is None


# ── тех-задание в свободном поле -> код-промпт (не грунтовка по резюме) ──
@pytest.mark.parametrize("prompt, is_code", [
    ("Напишите скрипт на Python для разворота строки", True),
    ("Реализуйте функцию сортировки", True),
    ("Составьте SQL-запрос по таблице users", True),
    ("Какой у вас опыт с Python?", False),
    ("Напишите о себе пару слов", False),
])
def test_is_code_task_detection(prompt, is_code):
    assert F.is_code_task(prompt) is is_code


def test_code_task_uses_code_prompt_and_keeps_code(monkeypatch):
    seen = {}
    _mock(monkeypatch, "```python\nprint('s'[::-1])\n```", seen)
    out = F.answer_field("Напишите скрипт на Python для разворота строки",
                         FieldType.TEXTAREA, (), "ctx")
    assert out == "print('s'[::-1])"                  # фенсы сняты, код проходит
    assert seen["system"] == F._CODE_SYSTEM           # экспертный код-промпт, не грунтовка
    assert "ФАКТЫ РЕЗЮМЕ" not in seen["user"]         # резюме в код-задание не уходит


def test_code_task_with_options_stays_grounded(monkeypatch):
    # «напишите...» с вариантами — это не свободное тех-задание: обычный путь + membership
    seen = {}
    _mock(monkeypatch, "Да", seen)
    assert F.answer_field("Напишите код на собеседовании — готовы?", FieldType.RADIO,
                          ("Да", "Нет"), "ctx") == "Да"
    assert seen["system"] == F._SYSTEM


def test_quiz_no_options_no_network(monkeypatch):
    called = []
    monkeypatch.setattr(F, "chat_json", lambda *a, **k: called.append(1))
    assert F.answer_quiz("Открытый вопрос", (), "ctx") is None
    assert called == []


def test_quiz_uses_expert_prompt(monkeypatch):
    seen = {}
    _mock(monkeypatch, "1", seen)
    F.answer_quiz("Q?", ("A", "B"), "ctx")
    assert "оценку" in seen["system"] and "DECLINE" in seen["system"] and "номер" in seen["system"]


# ── приватность: allowlist-контекст + скраб PII из resume.md ──
def test_resume_ctx_excludes_salary_and_citizenship():
    ctx = F.build_resume_ctx().lower()
    assert "salary" not in ctx and "граждан" not in ctx


# Полностью синтетический CV вместо личного personal/resume.md: тест герметичен (на чистом
# клоне/CI личного файла нет — вскрыто CI 23.07), PII-строки построены под _PII_LINE-триггеры,
# реальных данных не содержит.
_FAKE_CV = """# Иван Тестов
Гражданство: РФ, город проживания Приволжск
Телефон: +7 (900) 000-00-00, telegram @test_handle, почта candidate@example.com
Ожидания по зарплате: 200 000

## Опыт
Разработка API на Python/FastAPI, PostgreSQL.

## Образование
Высшее техническое.
"""


@pytest.fixture
def fake_resume_md(monkeypatch, tmp_path):
    md = tmp_path / "resume.md"
    md.write_text(_FAKE_CV, encoding="utf-8")
    monkeypatch.setattr(F, "_RESUME_MD", md)


@pytest.mark.parametrize("pii", ["граждан", "приволжск", "проживания", "+7 (900",
                                 "candidate@", "@test_handle"])
def test_resume_md_pii_scrubbed_from_ctx(fake_resume_md, pii):
    # resume.md идёт как контекст, но строки с PII (гражданство/город/контакты) не уходят наружу
    assert pii.lower() not in F.build_resume_ctx().lower()


def test_scrub_keeps_professional_context(fake_resume_md):
    # профессиональный контекст (навыки/опыт/образование) остаётся — иначе смысл resume.md теряется
    ctx = F.build_resume_ctx().lower()
    assert "fastapi" in ctx and "образование" in ctx


def test_payload_has_no_salary_citizenship(monkeypatch):
    seen = {}
    _mock(monkeypatch, "ok", seen)
    F.answer_field("вопрос", FieldType.TEXTAREA, (), F.build_resume_ctx())
    assert "salary" not in seen["user"].lower() and "граждан" not in seen["user"].lower()


# ── разметка form_answers как LLM-контекст: pii:true не уходит, остальное — уходит ──
def test_answers_ctx_includes_plain_excludes_pii(monkeypatch):
    monkeypatch.setattr(F, "load_profile", lambda: {"answers": {}, "form_answers": [
        {"q": "формат", "a": "Удалёнка", "_note": "формат работы"},
        {"q": "дат", "a": "02.02.1990", "_note": "дата рождения", "pii": True},
    ]})
    monkeypatch.setattr(F, "_RESUME_MD", pathlib.Path("nonexistent.md"))
    ctx = F.build_resume_ctx()
    assert "Удалёнка" in ctx and "УТВЕРЖДЁННЫЕ ОТВЕТЫ" in ctx
    assert "02.02.1990" not in ctx


def test_answers_ctx_scrub_is_second_net(monkeypatch):
    # запись с контактом БЕЗ pii-флага всё равно не уходит: строку режет _scrub_pii
    monkeypatch.setattr(F, "load_profile", lambda: {"answers": {}, "form_answers": [
        {"q": "ник", "a": "@secret_nick", "_note": "забыли флаг"},
    ]})
    monkeypatch.setattr(F, "_RESUME_MD", pathlib.Path("nonexistent.md"))
    assert "@secret_nick" not in F.build_resume_ctx()


# ── match_answer: словарь ответов (данные, не хардкод) ──
def test_match_answer_radio_membership(monkeypatch):
    monkeypatch.setattr(F, "form_answers", lambda: [{"q": r"офис|очн", "a": "Нет"}])
    assert F.match_answer("Готов к офисному формату?", ("Да", "Нет")) == ("Нет", None)


def test_match_answer_own_variant(monkeypatch):
    monkeypatch.setattr(F, "form_answers",
                        lambda: [{"q": r"курс|обуч", "a": "Свой вариант", "own": "Закончил"}])
    assert F.match_answer("На каком курсе?", ("1", "2", "Свой вариант")) == ("Свой вариант", "Закончил")


def test_match_answer_option_miss_is_none(monkeypatch):
    # ответ словаря не среди опций формы -> None (не подходит)
    monkeypatch.setattr(F, "form_answers", lambda: [{"q": r"час", "a": "40"}])
    assert F.match_answer("Часов?", ("полный день", "частичная")) is None


def test_match_answer_text_field(monkeypatch):
    monkeypatch.setattr(F, "form_answers", lambda: [{"q": r"город", "a": "Приволжск"}])
    assert F.match_answer("В каком городе?", ()) == ("Приволжск", None)


def test_match_answer_no_pattern(monkeypatch):
    monkeypatch.setattr(F, "form_answers", lambda: [{"q": r"офис", "a": "Нет"}])
    assert F.match_answer("Расскажите про опыт", ()) is None


# ── зарплата: детерминированный код (грейд -> ставка -> опция), НЕ LLM ──
@pytest.mark.parametrize("name,grade", [
    ("Стажёр-разработчик (Python)", F.Grade.JUNIOR),
    ("Junior Python Developer", F.Grade.JUNIOR),
    ("Backend разработчик (Python DRF)", F.Grade.MIDDLE),
    ("Ведущий Python-разработчик", F.Grade.SENIOR),
    ("Senior Python Engineer", F.Grade.SENIOR),
])
def test_detect_grade(name, grade):
    assert F.detect_grade(name) is grade


def _sbg(monkeypatch):
    monkeypatch.setattr(F, "load_profile", lambda: {"answers": {"salary_by_grade": {
        "junior": "90 000 — 100 000", "middle": "150 000 — 170 000", "senior": "200 000 — 220 000"}}})


def test_salary_target_by_grade(monkeypatch):
    _sbg(monkeypatch)
    assert F.salary_target("Backend разработчик (Python)") == 150000
    assert F.salary_target("Стажёр Python") == 90000
    assert F.salary_target("Ведущий Python") == 200000


def test_salary_target_vacancy_floor_wins(monkeypatch):
    _sbg(monkeypatch)
    assert F.salary_target("Backend разработчик", 180000) == 180000   # пол вакансии выше -> его
    assert F.salary_target("Backend разработчик", 120000) == 150000   # пол ниже -> грейд-ставка


@pytest.mark.parametrize("opt,low", [
    ("100к-150к", 100000), ("300к+", 300000), ("До 120 000 руб. на руки", 0),
    ("Более 120 000 руб. на руки", 120000), ("от 100 т.р. на руки", 100000),
    ("50–60 тысяч рублей", 50000), ("30–40 тысяч рублей", 30000),
    ("Любая оплата устроит, важно получить опыт", 0),
    ("не уверен (а), что заинтересован (а)", None), ("Свой вариант", None),
])
def test_salary_low_parse(opt, low):
    assert F._salary_low(opt) == low


def test_salary_option_picks_highest_qualified():
    opts = ("100к-150к", "150к-200к", "200к-300к", "300к+")
    assert F.salary_option(opts, 150000) == "150к-200к"      # наибольший вход <= target
    assert F.salary_option(opts, 250000) == "200к-300к"
    assert F.salary_option(opts, 90000) == "100к-150к"       # target ниже всех -> самый низкий


def test_salary_option_threshold_and_from():
    assert F.salary_option(("До 120 000 руб. на руки", "Более 120 000 руб. на руки"),
                           150000) == "Более 120 000 руб. на руки"
    assert F.salary_option(("Любая оплата устроит, важно опыт", "от 50 т.р. на руки",
                            "от 100 т.р. на руки"), 150000) == "от 100 т.р. на руки"


def test_salary_option_none_when_unparseable():
    assert F.salary_option(("Свой вариант", "не уверен"), 150000) is None


@pytest.mark.parametrize("q", [
    "Какие у вас ожидания по уровню заработной платы?", "На какой уровень дохода вы рассчитываете?",
    "Какую минимальную вилку зарплаты вы рассматриваете на старте?",
    "С какой заработной платы вы готовы начать работу после стажировки?",
])
def test_is_salary_q(q):
    assert F.is_salary_q(q)


def test_non_salary_not_flagged():
    assert not F.is_salary_q("Есть ли у вас военный билет?")


def test_salary_card_is_not_salary_q():
    # «зарплатная карта» — способ выплаты, не сумма -> не блокируем обычный путь (словарь/LLM)
    assert not F.is_salary_q("Как ты относишься к Альфа-банку как зарплатной карте?")


# ══════════════ security ══════════════
def test_injection_output_is_inert_string(monkeypatch):
    # инъекция в выходе LLM -> answer_field отдаёт просто СТРОКУ, ничего не исполняется
    payload = "__" + "import__('os').system('rm -rf ~')"
    _mock(monkeypatch, payload)
    out = F.answer_field("любой вопрос", FieldType.TEXTAREA, (), "ctx")
    assert out == payload and isinstance(out, str)


@pytest.mark.parametrize("prompt", [
    "Укажите пожалуйста ваши ожидания по уровню заработной платы?",
    "Какие у вас зарплатные ожидания (на руки)?",
    # ЖИВОЙ КЕЙС 27.07: формулировка без слова «зарплата» — поле уходило человеку, хотя
    # ставка по грейду известна и считается детерминированно (в LLM зарплата не уходит)
    "От каких сумм рассматриваете предложения о работе для себя?",
    "От какой суммы готовы рассматривать оффер?",
])
def test_salary_question_detected(prompt):
    assert F.is_salary_q(prompt) is True


@pytest.mark.parametrize("prompt", [
    "Расскажите о вашем опыте работы с Docker",
    "Готовы ли вы к командировкам?",
])
def test_non_salary_question_not_detected(prompt):
    assert F.is_salary_q(prompt) is False


@pytest.mark.parametrize("prompt", [
    # ЖИВОЙ КЕЙС 27.07: про сумму, но без слова «зарплата» — уходило человеку
    "Какие Ваши финансовые пожелания?",
    "Ваши финансовые ожидания?",
    "Какие денежные ожидания на испытательный срок?",
    # ЖИВОЙ КЕЙС 28.07: третья формулировка вилки без слова «зарплата»
    "На какую сумму вы сейчас рассматриваете предложения о работе?",
])
def test_money_wish_question_detected(prompt):
    assert F.is_salary_q(prompt) is True


@pytest.mark.parametrize("prompt", [
    # поле без вопроса вообще: работодателю нужен осмысленный ответ про ЭТУ вакансию,
    # поэтому класс мотивационный — отвечаем по описанию, а не заглушкой словаря
    "Прошу ответить тут :) Отклики без ответов просматриваться не будут.",
    "Отклики без ответов не рассматриваются",
])
def test_open_call_to_answer_is_motivation(prompt):
    assert F.is_motivation_q(prompt) is True


@pytest.mark.parametrize("prompt", [
    # ЖИВОЙ КЕЙС 27.07: «на какой уровень» без денег — это ГРЕЙД. Ветка ловила его как вилку и
    # вписала бы в вопрос о самооценке ставку по грейду; в radio ответ молча пропадал
    "На какой уровень ты себя оцениваешь как AI-инженер?",
    "На какой уровень позиции вы претендуете?",
    # выплата в валюте — это способ/валюта расчёта, а не сумма: путь словаря, не вилки
    "Готовы ли вы получать вознаграждение в EUR на https://volet.com?",
])
def test_grade_question_is_not_salary_question(prompt):
    assert F.is_salary_q(prompt) is False


@pytest.mark.parametrize("prompt", [
    "На какой уровень заработной платы вы претендуете?",
    "На какой уровень оплаты труда вы рассчитываете?",
])
def test_salary_level_question_still_detected(prompt):
    assert F.is_salary_q(prompt) is True


@pytest.mark.parametrize("module_name", ["form_fill", "form_read", "forms"])
def test_form_modules_have_no_exec_sink(module_name):
    # AST-гвард (не строковый скан — иначе ловил бы токены в docstring): в form_*/forms нет
    # вызовов eval/exec/compile/os.system/os.popen/__import__ и импорта subprocess (RFC-003 L1).
    # Модуль — параметр, а не элемент цикла: падение называет модуль, а не «первый из трёх».
    # Обход дерева циклом остаётся — это один вход, а не набор случаев.
    mod = importlib.import_module(f"hrwork.application.apply.forms.{module_name}")
    banned_call = {"eval", "exec", "compile", "__import__"}
    banned_attr = {"system", "popen"}
    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                assert fn.id not in banned_call, f"{module_name}: {fn.id}()"
            if isinstance(fn, ast.Attribute):
                assert fn.attr not in banned_attr, f"{module_name}: .{fn.attr}()"
        if isinstance(node, ast.Import):
            assert all(a.name != "subprocess" for a in node.names), f"{module_name}: subprocess"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "subprocess", f"{module_name}: from subprocess"


# ── «Чем интересна ваша компания»: отвечается по описанию ВАКАНСИИ ──
@pytest.mark.parametrize("prompt", [
    "Что привлекло твое внимание к нашей компании?",
    "Чем вас заинтересовала эта вакансия?",
    "Опишите, пожалуйста, почему вы считаете, что вы соответствуете нашим требованиям.",
])
def test_motivation_question_detected(prompt):
    assert F.is_motivation_q(prompt) is True


@pytest.mark.parametrize("prompt", [
    "Какие у вас зарплатные ожидания?",
    "Укажите ваш возраст.",
    "Опыт работы с Docker?",
])
def test_plain_question_is_not_motivation(prompt):
    assert F.is_motivation_q(prompt) is False


def test_motivation_uses_vacancy_text(monkeypatch):
    seen = {}
    _mock(monkeypatch, "Интересны задачи по нагрузочному тестированию на Python.", seen)
    out = F.answer_motivation("Чем заинтересовала вакансия?",
                              "Ищем инженера: нагрузочное тестирование, Python, k6", "стек: Python")
    assert out == "Интересны задачи по нагрузочному тестированию на Python."
    assert "k6" in seen["user"]                    # описание вакансии реально ушло в промпт
    assert "ОПИСАНИЕ ВАКАНСИИ" in seen["user"]


def test_motivation_without_vacancy_text_no_network(monkeypatch):
    # без описания отвечать нечем — в сеть не ходим, поле уходит человеку
    called = []
    monkeypatch.setattr(F, "chat_json", lambda *a, **k: called.append(1))
    assert F.answer_motivation("Чем заинтересовала вакансия?", "", "ctx") is None
    assert F.answer_motivation("Чем заинтересовала вакансия?", "текст", "  ") is None
    assert called == []


def test_motivation_decline_returns_none(monkeypatch):
    _mock(monkeypatch, "DECLINE")
    assert F.answer_motivation("Чем интересна вакансия?", "описание", "ctx") is None
