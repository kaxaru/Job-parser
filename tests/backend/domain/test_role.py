"""Тесты VO Role: is_it, label, и страж дрейфа с config.ROLE_PATTERNS."""
import pytest

from hrwork.config import ROLE_PATTERNS
from hrwork.domain.role import Role


@pytest.mark.parametrize("role, expected", [
    (Role.BACKEND, True),
    (Role.QA, True),
    (Role.DATA_ML, True),
    (Role.DEVELOPER, True),
    (Role.NON_IT, False),
], ids=lambda x: x.name if isinstance(x, Role) else str(x))
def test_is_it_true_except_non_it(role, expected):
    # цепочка через `and` падала целиком и не называла виновную роль
    assert role.is_it is expected


def test_label_is_display_string():
    assert Role.NON_IT.label == "Не-IT"
    assert Role.ANALYST.label == "Аналитик"
    assert Role.DATA_ML.label == "Data/ML"


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.name)
def test_from_label_roundtrips(role):
    assert Role.from_label(role.label) is role


# Страж дрейфа с config.ROLE_PATTERNS — теперь ДВУСТОРОННИЙ (аудит 09.08.2026). Прежняя
# форма сверяла только включение `ROLE_PATTERNS ⊆ Role.labels` и обобщённым `assert not
# missing`: УДАЛЕНИЕ ключа (например, 'Security') она не ловила вовсе, а в проде вакансии
# по ИБ молча падали бы в «Разработчик»/«Не-IT» — то же расхождение, ради которого страж
# и заведён, только с другой стороны.

def test_every_role_pattern_has_a_role_member():
    # Литерал по спеке (docs/testing.md, «Тесты-стражи»): 17 ключей паттернов.
    assert sorted(ROLE_PATTERNS) == sorted([
        "Mobile", "QA", "DevOps", "Data Eng", "GenAI", "Data/ML", "Аналитик", "Embedded",
        "Security", "Gamedev", "Architect", "Дизайнер", "Frontend", "Backend",
        "Fullstack", "Менеджер", "Разработчик"])


def test_genai_is_matched_before_data_ml():
    """Порядок ключей — единственное, что разводит GenAI и Data/ML: выигрывает первое
    совпадение, а генеративные токены намеренно оставлены и в Data/ML (вынимать их оттуда
    нельзя, см. config). Перестановка ключей местами тихо обнулила бы новую роль."""
    keys = list(ROLE_PATTERNS)
    assert keys.index("GenAI") < keys.index("Data/ML")


def test_non_it_is_the_only_role_without_a_pattern():
    # 18 членов Role против 17 ключей: NON_IT служебный, паттерна у него нет
    assert {r.label for r in Role} - set(ROLE_PATTERNS) == {"Не-IT"}
