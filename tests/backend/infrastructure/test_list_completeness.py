"""Сверка полноты списка — ОДНА функция на все постраничные адаптеры (`sources/base.py`).

Правило «страница, не отдавшаяся после всех ретраев, не превращается в тихую потерю»
(аудит 08.08.2026, находка 14) нужно трём порталам сразу, и первая волна фиксов завела его
тремя копиями — класс `ListIncomplete` и почти одинаковую проверку в hirify, talanto и
getmatch. Дубль между адаптерами — ровно тот класс инцидента, ради которого в проекте и
заведён домен: копии расходятся тихо, а «список собран не полностью» обязано значить одно
и то же на всех порталах.

Разным у порталов остаётся ровно одно — ПОРОГ допустимой потери, и он передаётся
параметром: hirify и talanto 2 % (портал на 18-40k записей), getmatch 25 % (портал на ~740,
где одна страница это 13.5 %). Обоснование каждого живёт рядом с ним, в конфиге адаптера.
"""
import pytest

from hrwork.infrastructure.sources import base, getmatch, hirify, talanto
from hrwork.infrastructure.sources.base import ListIncomplete, check_list_complete

#: пороги спецификации — литералы, а не CFG порталов: тест обязан упасть, если порог сдвинут
STRICT = 0.02       # hirify / talanto
GROSS = 0.25        # getmatch


class _Log:
    """Перехват строк лога: у loguru формат — str.format с позиционными аргументами."""

    def __init__(self):
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg.format(*args))

    def info(self, msg, *args):
        self.infos.append(msg.format(*args))

    def debug(self, msg, *args):
        pass


# ── порог — параметр, а не константа функции ────────────────────────────────────────────

def test_loss_above_the_strict_threshold_discards_the_run():
    # 4 страницы по 100 из 18 000 = 2.2 % — для hirify это дороже пропуска прогона
    with pytest.raises(ListIncomplete):
        check_list_complete(17600, total=18000, per_page=100, failed=[1, 2, 3, 4],
                            source="hirify", max_lost_ratio=STRICT)


def test_the_same_loss_is_tolerated_by_a_portal_with_a_grosser_threshold(monkeypatch):
    # Та же потеря в 2.2 % для getmatch — штатный WARNING: уронить его прогон значит
    # выбросить многочасовой сбор остальных восьми источников
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    check_list_complete(17600, total=18000, per_page=100, failed=[1, 2, 3, 4],
                        source="getmatch", max_lost_ratio=GROSS)
    assert len(fake.warnings) == 1


def test_loss_exactly_at_the_threshold_discards_the_run():
    # 2 страницы по 100 из 800 = ровно 25 %: порог включающий
    with pytest.raises(ListIncomplete):
        check_list_complete(600, total=800, per_page=100, failed=[100, 200],
                            source="getmatch", max_lost_ratio=GROSS)


def test_the_error_states_the_loss_the_threshold_and_the_pages():
    with pytest.raises(ListIncomplete) as e:
        check_list_complete(17600, total=18000, per_page=100, failed=[1, 2, 3, 4],
                            source="hirify", max_lost_ratio=STRICT)
    assert str(e.value) == ("страниц не отдалось 4 (~400 записей, 2.2% от 18000); "
                            "порог 2%; страницы: 1, 2, 3, 4")


# ── сообщение называет портал и тот идентификатор, по которому запрос воспроизводится ───

def test_warning_names_the_source_and_the_failed_pages(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    check_list_complete(17900, total=18000, per_page=100, failed=[41],
                        source="hirify", max_lost_ratio=STRICT)
    assert fake.warnings == [
        "hirify: страниц не отдалось 1 (~100 записей, 0.6% от 18000) — срез неполный, "
        "страницы: 41"]


def test_warning_names_offsets_for_an_adapter_that_walks_by_offset(monkeypatch):
    # talanto и getmatch ходят по offset'ам; «страница 12000» оператора обманула бы
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    check_list_complete(39900, total=40000, per_page=100, failed=[12000],
                        source="talanto", max_lost_ratio=STRICT, failed_kind="offset'ы")
    assert fake.warnings == [
        "talanto: страниц не отдалось 1 (~100 записей, 0.2% от 40000) — срез неполный, "
        "offset'ы: 12000"]


def test_only_the_first_five_failures_are_listed(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    check_list_complete(17300, total=180000, per_page=100, failed=[7, 6, 5, 4, 3, 2, 1],
                        source="hirify", max_lost_ratio=STRICT)
    assert fake.warnings[0].endswith("страницы: 1, 2, 3, 4, 5")


# ── недобор без сбойных страниц — норма ─────────────────────────────────────────────────

def test_shortfall_without_failed_pages_is_reported_as_a_shift_not_a_loss(monkeypatch):
    # Выдача сдвигается между запросами (сортировка по свежести): это INFO, не WARNING
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    check_list_complete(17990, total=18000, per_page=100, failed=[],
                        source="hirify", max_lost_ratio=STRICT)
    assert fake.warnings == []
    assert fake.infos == ["hirify: список 17990 из заявленных 18000 — выдача сдвинулась "
                          "между запросами"]


def test_complete_list_says_nothing(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    check_list_complete(18000, total=18000, per_page=100, failed=[],
                        source="hirify", max_lost_ratio=STRICT)
    assert (fake.warnings, fake.infos) == ([], [])


def test_unknown_total_treats_any_failed_page_as_a_total_loss():
    # total=0 (портал его не отдал) -> доля не считается, срез считаем непригодным целиком
    with pytest.raises(ListIncomplete):
        check_list_complete(100, total=0, per_page=100, failed=[2],
                            source="hirify", max_lost_ratio=STRICT)


# ── страж: адаптеры не заводят свою копию ───────────────────────────────────────────────

@pytest.mark.parametrize("module", [hirify, talanto, getmatch])
def test_adapter_keeps_no_private_copy_of_the_check(module):
    """Копия проверки в адаптере — то, как дефект и приехал: три почти одинаковых функции
    и три разных класса `ListIncomplete`, которые невозможно поймать одним `except`."""
    assert "_check_list_complete" not in vars(module)


@pytest.mark.parametrize("module", [hirify, talanto, getmatch])
def test_adapter_keeps_no_private_copy_of_the_exception(module):
    assert "ListIncomplete" not in vars(module)
