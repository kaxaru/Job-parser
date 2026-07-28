"""Капча HH останавливает прогон, а не притворяется архивной вакансией.

ИНЦИДЕНТ 28.07: HH увёл браузер на `/account/captcha?backurl=...` — собственная капча аккаунта
(картинка `[data-qa="account-captcha-picture"]`), не DDoS-Guard. Страница проходит `_goto`
(корень HH на ней есть), но кнопки отклика и полей анкеты на ней нет, поэтому:
  * apply-путь писал «Пропуск (внешний/архив)» и перемолол 35 карточек за 50 минут до watchdog;
  * прогон форм писал «анкета без извлечённых полей» и прошёл полсотни анкет вхолостую;
  * свип записал бы живые анкеты как EMPTY, а `--clean` вычистил бы по этому признаку очередь.
Каждый следующий клик под капчей — ещё один бот-сигнал, поэтому исход отдельный и он рвёт цикл.
"""
import pytest

from hrwork.application.apply import autoclick
from hrwork.application.apply.outcome import ApplyOutcome


class _Page:
    """Страница, у которой важен только итоговый URL после редиректов."""

    def __init__(self, url: str):
        self.url = url


@pytest.mark.parametrize("url, expected", [
    ("https://hh.ru/account/captcha?backurl=https%3A%2F%2Fhh.ru%2Fvacancy%2F135307886", True),
    ("https://hh.ru/account/captcha", True),
    ("https://hh.ru/vacancy/135307886", False),
    ("https://hh.ru/applicant/vacancy_response?vacancyId=135307886", False),
    ("https://hh.ru/search/vacancy?text=python", False),
    ("", False),
])
def test_captcha_page_detected_by_url(url, expected):
    assert autoclick.is_captcha(_Page(url)) is expected


def test_unreadable_page_is_not_captcha():
    # во время навигации контекст рушится и чтение url бросает — это не повод объявлять капчу
    class _Broken:
        @property
        def url(self):
            raise RuntimeError("Execution context was destroyed")

    assert autoclick.is_captcha(_Broken()) is False


def test_captcha_outcome_code():
    # код исхода уходит в лог и wire — фиксируем литералом
    assert ApplyOutcome.CAPTCHA.code == "captcha"
