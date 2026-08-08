"""Допустимый опыт отбора: непризнанный код не молчит.

`candidates.py::APPLY_EXPS` собирает грейды отбора мягким парсером (`Experience.from_code`),
применённым к НАШЕЙ константе из профиля, а не к данным портала. Молчаливый пропуск здесь
стоит дорого и невидим: пул кандидатов просто оказывается меньше, чем должен быть, — это
недоотклики без единого признака в логе (аудит 08.08.2026, вторая половина находки 30).
"""
import pytest

from hrwork.application.apply import candidates as C
from hrwork.domain.experience import Experience


@pytest.fixture
def log_lines():
    """Строки, ушедшие в лог за тест. Свой сток, потому что loguru не пишет в `caplog`."""
    lines: list[str] = []
    sink = C.log.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    yield lines
    C.log.remove(sink)


def test_known_codes_become_experience_values(log_lines):
    assert C._exps_from_codes(["noExperience", "between3And6"]) == {
        Experience.NONE, Experience.BETWEEN_3_6}
    assert log_lines == []


def test_unrecognized_code_is_named_in_the_log(log_lines):
    assert C._exps_from_codes(["noExperience", "betwen1And3"]) == {Experience.NONE}
    assert log_lines == ["APPLY_EXPS: код опыта betwen1And3 не распознан — исключён из отбора"]


def test_every_unrecognized_code_gets_its_own_line(log_lines):
    # одна жалоба на список скрыла бы вторую опечатку до следующей правки профиля
    assert C._exps_from_codes(["betwen1And3", "moreThan7"]) == set()
    assert log_lines == [
        "APPLY_EXPS: код опыта betwen1And3 не распознан — исключён из отбора",
        "APPLY_EXPS: код опыта moreThan7 не распознан — исключён из отбора"]


def test_live_selection_uses_only_recognized_grades():
    """Действующий набор — это VO, а не сырые строки: сравнение с `v.experience`
    в `pick_candidates` иначе не совпало бы никогда."""
    assert [e for e in C.APPLY_EXPS if not isinstance(e, Experience)] == []
