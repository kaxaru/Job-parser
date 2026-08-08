"""Маскировщик тел (`config.py::body`) реально ВКЛЮЧЁН в точках, где лог видел переписку.

Инцидент 08.08.2026 (аудит роем, п.49): `logs/*.log` и `logs/cron_*.log` были готовым дампом
переписки — вопрос рекрутера, полный отправляемый ответ с фактами профиля и пары «поле анкеты
-> подставленное значение» вместе с зарплатой. Уровнем синка это не лечится: строки пишутся
`log.info`, а не `log.debug`, поэтому маскировка стоит в точке вызова.

Сам `body` покрыт в `tests/backend/test_log_masking.py`; здесь проверяется ПРОВОДКА: что
дословного текста в сообщении лога нет, что служебная часть строки (id вакансии, правило,
тип поля, счётчики) осталась и что `LOG_BODIES=1` возвращает дословную запись.
"""
import pytest

from hrwork import config as C
from hrwork.application.apply.chat import chat_reply
from hrwork.application.apply.forms import forms
from hrwork.application.apply.forms.form_read import FieldType, FormField

_Q = "Здравствуйте! Расскажите про ваш опыт с Kafka в проде"   # 53 симв.
_A = "Да, работал с Kafka: писал консьюмеров на Python."       # 49 симв.
_C = "Опыт с Kafka есть: писал консьюмеров на Python."         # 47 симв.


@pytest.fixture(autouse=True)
def masked(monkeypatch):
    """Дефолт эксплуатации: дословных тел в логе нет."""
    monkeypatch.setattr(C, "LOG_BODIES", False)


@pytest.fixture
def log_lines():
    """Строки, ушедшие в лог за тест. Свой сток, потому что loguru не пишет в `caplog`."""
    lines: list[str] = []
    sink = C.log.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    yield lines
    C.log.remove(sink)


def _proposal(manual=False):
    return chat_reply.Proposal(vid="777", chat_id=5, question=_Q, text=_A,
                               rule="has_exp_yes", lang="ru", sender="bot", manual=manual)


@pytest.fixture
def planned(monkeypatch):
    """Подменить план прогона: dry-run печатает предложения, не трогая диск и сеть."""
    def _set(sendable, manual):
        monkeypatch.setattr(chat_reply, "_plan",
                            lambda only, limit, classify: (sendable, manual))
    return _set


# ── чаты: вопрос рекрутера и отправляемый ответ ──
def test_dry_run_logs_the_shape_of_the_answer_not_its_text(planned, log_lines):
    planned([_proposal()], [])
    chat_reply.run(send=False)
    assert log_lines[0] == ("[has_exp_yes] bot #777: «Здравствуйте! Расска… <53 симв.>» "
                            "-> «<49 симв.>»")


def test_rephrase_preview_masks_both_source_and_candidate(monkeypatch, planned, log_lines):
    # человеку показывают ДВА тела сразу — источник и кандидата; дословно уходили оба
    monkeypatch.setattr(chat_reply, "_make_rephraser", lambda: (lambda q, s, r, lg: _C))
    planned([_proposal()], [])
    chat_reply.run(send=False, use_rephrase=True)
    assert log_lines[0] == ("[has_exp_yes] #777: «Здравствуйте! Расска… <53 симв.>»\n"
                            "    источник: «<49 симв.>»\n"
                            "    кандидат: «<47 симв.>»")


def test_manual_proposal_is_masked_too(planned, log_lines):
    # деньги/место идут именно этой веткой — самое чувствительное из того, что мы пишем
    planned([], [_proposal(manual=True)])
    chat_reply.run(send=False)
    assert log_lines[0] == ("[manual, НЕ отправляется] #777: «Здравствуйте! Расска… <53 симв.>» "
                            "-> предложение: «<49 симв.>»")


def test_log_bodies_flag_restores_the_verbatim_dialog(monkeypatch, planned, log_lines):
    monkeypatch.setattr(C, "LOG_BODIES", True)
    planned([_proposal()], [])
    chat_reply.run(send=False)
    assert log_lines[0] == f"[has_exp_yes] bot #777: «{_Q}» -> «{_A}»"


# ── анкеты: превью «поле -> подставленное значение» ──
@pytest.fixture
def salary_preview(monkeypatch):
    """Превью одной анкеты с зарплатным полем: значение подставляет код (`salary_target`)."""
    fld = FormField(selector='input[name="task_1"]', name="task_1",
                    prompt="Укажите ваши зарплатные ожидания", ftype=FieldType.TEXT)
    monkeypatch.setattr(forms.form_read, "extract_fields", lambda _page: [fld])
    monkeypatch.setattr(forms, "_vacancy_ctx", lambda _vid: ("", "", ""))
    monkeypatch.setattr(forms, "_vacancy_floor", lambda _vid: None)
    monkeypatch.setattr(forms, "_vacancy_exp", lambda _vid: None)
    monkeypatch.setattr(forms.form_fill, "salary_target", lambda *_a: 250_000)


def test_form_preview_hides_the_salary_and_keeps_the_field_type(salary_preview, log_lines):
    # «от 250 000 руб.» — 15 симв.; в логе остаётся только форма ответа и служебная часть
    forms._dry_preview(None, "777", {"name": "Python-разработчик"})
    assert log_lines == [
        "  [text] Укажите ваши зарплатные о… <32 симв.> -> <15 симв.>",
        "[777] Python-разработчик — полей 1, пробелов 0"]


def test_form_preview_verbatim_under_the_debug_flag(monkeypatch, salary_preview, log_lines):
    monkeypatch.setattr(C, "LOG_BODIES", True)
    forms._dry_preview(None, "777", {"name": "Python-разработчик"})
    assert log_lines[0] == "  [text] Укажите ваши зарплатные ожидания -> от 250 000 руб."
