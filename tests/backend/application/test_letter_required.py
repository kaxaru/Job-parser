"""Обязательное сопроводительное не должно превращать живую вакансию в «архив».

ИНЦИДЕНТ 03.08.2026: часть работодателей помечает письмо обязательным. Пока поле пустое,
HH отдаёт кнопку «Откликнуться» с атрибутом `disabled`; клик по ней падает по таймауту,
исключение подавлено, подтверждение не приходит — и `apply_one` возвращал SKIP МОЛЧА.
В логе это выглядело как «Пропуск (внешний/архив)»: на 50 пропусков приходилось 3 строки
диагностики, остальные 47 не оставляли ничего.

Проверка по HH показала, что 47 из 50 «архивных» были живые (`active=true archived=false`),
а на выборке из 11 кнопка сабмита была `disabled` во всех 11 случаях. Отношение пропусков
к откликам за три недели выросло с 2.8 до 62.2, и отклики упали со 117 в день до 21.

Правило: письмо вписываем ТОЛЬКО когда кнопка disabled. Там, где HH пускает и так, поведение
прежнее — письмо уходит в слот сопроводительного через chatik уже ПОСЛЕ отклика.
"""
from typing import Any

import pytest

from hrwork.application.apply import autoclick
from hrwork.application.apply.candidates import Candidate

# письмо из шаблона для кандидата ниже — ожидание из спецификации cover.COVER_TEMPLATE
EXPECTED_TEMPLATE_LETTER = ("Здравствуйте! Заинтересовала вакансия Python-разработчик "
                            "в компании ООО Ромашка. Буду рад обсудить детали.")

CAND = Candidate(id="135096772", name="Python-разработчик",
                 url="https://hh.ru/vacancy/135096772", employer="ООО Ромашка")


class _Locator:
    """Локатор Playwright в объёме, который трогает _fill_letter_if_required."""

    def __init__(self, count: int = 1, disabled: bool = False, boom: bool = False):
        self._count, self._disabled, self._boom = count, disabled, boom
        self.filled: str | None = None

    @property
    def first(self) -> "_Locator":
        return self

    def count(self) -> int:
        if self._boom:
            raise RuntimeError("Execution context was destroyed")
        return self._count

    def is_disabled(self, timeout: int | None = None) -> bool:
        return self._disabled

    def fill(self, text: str, timeout: int | None = None) -> None:
        self.filled = text


class _Page:
    def __init__(self, submit: _Locator, letter: _Locator):
        self._by_selector = {autoclick._RESPONSE_SUBMIT: submit,
                             autoclick._RESPONSE_LETTER: letter}

    def locator(self, selector: str) -> Any:
        return self._by_selector[selector]


def test_disabled_submit_with_empty_letter_gets_template_cover():
    submit, letter = _Locator(disabled=True), _Locator()
    filled = autoclick._fill_letter_if_required(_Page(submit, letter), CAND)
    assert filled is True
    assert letter.filled == EXPECTED_TEMPLATE_LETTER


def test_explicit_cover_text_wins_over_template():
    # путь из ленты передаёт письмо, отредактированное человеком — оно и должно уйти
    submit, letter = _Locator(disabled=True), _Locator()
    autoclick._fill_letter_if_required(_Page(submit, letter), CAND, cover_text="Моё письмо")
    assert letter.filled == "Моё письмо"


def test_active_submit_leaves_letter_untouched():
    # HH пускает без письма -> ничего не трогаем, письмо уйдёт в чат после отклика
    submit, letter = _Locator(disabled=False), _Locator()
    filled = autoclick._fill_letter_if_required(_Page(submit, letter), CAND)
    assert filled is False
    assert letter.filled is None


@pytest.mark.parametrize("submit, letter, case", [
    (_Locator(disabled=True), _Locator(count=0), "disabled, но поля письма нет"),
    (_Locator(count=0), _Locator(), "кнопки сабмита нет вовсе"),
    (_Locator(boom=True), _Locator(), "DOM недоступен — контекст разрушен"),
])
def test_not_our_case_returns_false(submit, letter, case):
    assert autoclick._fill_letter_if_required(_Page(submit, letter), CAND) is False, case
    assert letter.filled is None, case


def test_blank_cover_text_falls_back_to_template():
    # пробелы не снимут disabled, но затрут поле — вместо них идёт шаблон
    submit, letter = _Locator(disabled=True), _Locator()
    autoclick._fill_letter_if_required(_Page(submit, letter), CAND, cover_text="   \n  ")
    assert letter.filled == EXPECTED_TEMPLATE_LETTER
