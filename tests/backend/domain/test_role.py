"""Тесты VO Role: is_it, label, и страж дрейфа с config.ROLE_PATTERNS."""
from hrwork.config import ROLE_PATTERNS
from hrwork.domain.role import Role


def test_is_it_true_except_non_it():
    assert Role.BACKEND.is_it and Role.QA.is_it and Role.DATA_ML.is_it
    assert Role.DEVELOPER.is_it
    assert Role.NON_IT.is_it is False


def test_label_is_display_string():
    assert Role.NON_IT.label == "Не-IT"
    assert Role.ANALYST.label == "Аналитик"
    assert Role.DATA_ML.label == "Data/ML"


def test_from_label_roundtrips():
    for r in Role:
        assert Role.from_label(r.label) is r


def test_role_patterns_keys_are_all_roles():
    # страж дрейфа: каждый ключ config.ROLE_PATTERNS обязан иметь член Role
    # (иначе _detect_role.from_label упадёт ValueError на новой роли).
    labels = {r.label for r in Role}
    missing = [k for k in ROLE_PATTERNS if k not in labels]
    assert not missing, f"ROLE_PATTERNS без члена Role: {missing}"
