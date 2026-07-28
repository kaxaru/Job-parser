"""Мёртвая анкета замораживает вакансию до переоткрытия (form_status.py)."""
import pytest

from hrwork.application.apply.forms.form_status import (
    FormSweepStatus,
    is_revived,
    skippable_form_ids,
)

SWEPT = "2026-07-27T14:00:00+04:00"


@pytest.mark.parametrize("published, expected", [
    ("2026-07-28T09:00:00+04:00", True),    # переоткрыли на следующий день после свипа
    ("2026-07-27T14:00:01+04:00", True),    # на секунду позже — уже другая публикация
    ("2026-07-27T13:59:59+04:00", False),   # публикация ДО свипа — та же мёртвая вакансия
    ("2026-07-01T10:00:00+04:00", False),
    ("", False),                            # даты нет — считаем, что не оживала
    (None, False),
    ("мусор", False),                       # кеш и выдача — внешние данные, не падаем
])
def test_is_revived(published, expected):
    assert is_revived({"ts": SWEPT}, published) is expected


def test_is_revived_without_sweep_timestamp():
    # старая запись кеша без ts: ожить не может, иначе вернём в оборот снятую вакансию
    assert is_revived({}, "2026-07-28T09:00:00+04:00") is False
    assert is_revived(None, "2026-07-28T09:00:00+04:00") is False


def test_live_form_is_always_skipped():
    # живую анкету бот не заполняет (вопросы работодателя специфичны) — пропускаем всегда,
    # даже если вакансию только что переопубликовали
    cache = {"1": {"status": FormSweepStatus.OK.code, "ts": SWEPT}}
    assert skippable_form_ids(["1"], cache, {"1": "2026-07-28T09:00:00+04:00"}) == {"1"}


def test_dead_form_stays_skipped_until_republished():
    cache = {"1": {"status": FormSweepStatus.EMPTY.code, "ts": SWEPT}}
    assert skippable_form_ids(["1"], cache, {"1": "2026-07-20T09:00:00+04:00"}) == {"1"}


def test_dead_form_returns_to_pool_after_republish():
    # ЗАЧЕМ: мёртвая анкета = вакансия снята. HH позволяет переоткрыть ту же вакансию, и
    # тогда форма оживает — без этого правила она осталась бы в пропуске навсегда.
    cache = {"1": {"status": FormSweepStatus.EMPTY.code, "ts": SWEPT}}
    assert skippable_form_ids(["1"], cache, {"1": "2026-07-28T09:00:00+04:00"}) == set()


def test_error_status_behaves_like_dead():
    cache = {"1": {"status": FormSweepStatus.ERROR.code, "ts": SWEPT}}
    assert skippable_form_ids(["1"], cache, {"1": "2026-07-28T09:00:00+04:00"}) == set()
    assert skippable_form_ids(["1"], cache, {"1": "2026-07-26T09:00:00+04:00"}) == {"1"}


def test_unswept_form_is_skipped():
    # анкета в очереди, но свип до неё не дошёл: пропускаем — вдруг живая
    assert skippable_form_ids(["1"], {}, {"1": "2026-07-28T09:00:00+04:00"}) == {"1"}
