"""Разбор кода входа hh.ru из текста SMS-уведомления (RFC-004) — `apply/sms.py`.

`extract_code` — чистая функция: только вход (текст уведомления) и результат (код или None).
Ложное срабатывание опаснее пропуска: код уходит в форму входа, чужое число из другого
уведомления не должно им притвориться, поэтому число берётся только рядом с «hh.ru»/«код».
Номера в текстах вымышленные.
"""
import pytest

from hrwork.application.apply import sms


@pytest.mark.parametrize("text, expected", [
    ("hh.ru: ваш код 1234", "1234"),
    ("Kod hh.ru: 7469. Nikomu ne soobshchayte", "7469"),
    ("Your hh.ru code is 4321", "4321"),
    ("hh.ru, ваш проверочный код для входа: 5678", "5678"),   # длинный текст -> ловит «код»
    ("Код для входа: 9999. Никому не сообщайте", "9999"),
    ("code: 246810", "246810"),
    ("hh.ru код 12", None),                                    # 2 цифры < 4 — не код
    ("Заказ 12345 доставлен", None),                           # число без hh.ru/код — не наше
    ("Уведомление без цифр", None),
    ("", None),
])
def test_extract_code(text, expected):
    assert sms.extract_code(text) == expected


def test_read_login_code_degrades_to_none_without_db(monkeypatch):
    """Нет базы уведомлений (не Windows / нет «Связи с телефоном») -> None, без ожидания."""
    monkeypatch.setattr(sms, "notifications_db", lambda: None)
    assert sms.read_login_code(after_id=0, timeout_s=0.0) is None


def test_latest_notification_id_is_zero_without_db(monkeypatch):
    monkeypatch.setattr(sms, "notifications_db", lambda: None)
    assert sms.latest_notification_id() == 0
