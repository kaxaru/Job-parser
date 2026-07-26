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


def test_role_patterns_keys_are_all_roles():
    # страж дрейфа: каждый ключ config.ROLE_PATTERNS обязан иметь член Role
    # (иначе _detect_role.from_label упадёт ValueError на новой роли).
    labels = {r.label for r in Role}
    missing = [k for k in ROLE_PATTERNS if k not in labels]
    assert not missing, f"ROLE_PATTERNS без члена Role: {missing}"
