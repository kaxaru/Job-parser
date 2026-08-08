"""Маскирование тел сообщений в логе (`config.py::body`).

Инцидент 08.08.2026 (аудит роем, п.49): `logs/*.log` и `logs/cron_*.log` оказались готовым
дампом переписки — вопрос рекрутера, полный отправляемый ответ с фактами профиля и пары
«поле анкеты -> подставленное значение» с зарплатой. Предложенный аудитом фикс «файловый
синк на INFO» находку НЕ закрывал: перечисленные строки пишутся `log.info`, а не `log.debug`.
Поэтому маскировка живёт в точке вызова, а `LOG_BODIES=1` возвращает дословную запись.
"""
import pytest

from hrwork import config as C


@pytest.fixture(autouse=True)
def _masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дефолт эксплуатации: дословных тел в логе нет."""
    monkeypatch.setattr(C, "LOG_BODIES", False)


@pytest.mark.parametrize(("text", "expected"), [
    ("Здравствуйте! Расскажите про опыт с Kafka", "<41 симв.>"),
    ("Да, работал с Kafka в проде", "<27 симв.>"),
    ("", "<пусто>"),
    ("x", "<1 симв.>"),
])
def test_body_replaces_message_with_its_length(text: str, expected: str) -> None:
    assert C.body(text) == expected


def test_body_keeps_requested_prefix_to_tell_fields_apart() -> None:
    # keep нужен там, где без первых символов не отличить одно поле анкеты от другого
    assert C.body("Готовы ли вы к переезду в другой город?", keep=10) == "Готовы ли … <39 симв.>"


def test_body_shorter_than_keep_is_still_masked() -> None:
    # строка короче keep целиком видима не становится — иначе короткий ответ утечёт дословно
    assert C.body("Да", keep=10) == "<2 симв.>"


def test_body_stringifies_non_str_before_measuring() -> None:
    assert C.body(250000) == "<6 симв.>"


def test_log_bodies_flag_restores_verbatim_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C, "LOG_BODIES", True)
    assert C.body("Расскажите про опыт с Kafka", keep=5) == "Расскажите про опыт с Kafka"
