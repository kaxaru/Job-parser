"""Подготовка прогона ДО импорта пакета: sys.path и фолбэк профиля резюме.

Фолбэк профиля стоит здесь, а не фикстурой, сознательно (аудит 08.08.2026, находка 63).
`hrwork/config.py` читает `resume_profile.json` НА ИМПОРТЕ, а импортируется он при СБОРКЕ
тестовых модулей. Session-autouse фикстура копировала пример при setup ПЕРВОГО теста —
config к тому моменту уже испёкся на `_RESUME_DEFAULT`, и на чистом клоне один прогон жил
на двух разных профилях: config на дефолтах, а движки ответов (читают файл лениво) — на
примере. `pytest_configure` вызывается до сборки, то есть до первого `import hrwork.config`.

Порядок не предполагается, а СТЕРЕЖЁТСЯ: флаг `_config_imported_at_configure` уходит в
tests/backend/test_profile_isolation.py::test_profile_fallback_precedes_config_import.
Из-за него ни один conftest НЕ ИМЕЕТ ПРАВА импортировать `hrwork` на уровне модуля:
conftest'ы стартовых путей (`testpaths = tests/backend`) грузятся РАНЬШЕ `pytest_configure`.

Живой профиль разработчика не трогается и не читается — только проверяется его наличие.
"""
import shutil
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

_REAL = _ROOT / "resume_profile.json"
_EXAMPLE = _ROOT / "resume_profile.example.json"

_fallback_installed = False          # пример подложен нами -> убрать за собой
_config_imported_at_configure = True  # пессимистичный дефолт: страж должен краснеть, а не молчать


def pytest_configure(config: pytest.Config) -> None:
    """Подложить обезличенный пример, если персонального профиля нет (чистый клон, CI).

    Без профиля 12 тестов chat_reply/form_fill падали пустыми ответами (вскрыто CI
    23.07.2026)."""
    global _fallback_installed, _config_imported_at_configure
    _config_imported_at_configure = "hrwork.config" in sys.modules
    if _REAL.exists() or not _EXAMPLE.exists():
        return                       # профиль есть (или подложить нечего) — ничего не делаем
    shutil.copyfile(_EXAMPLE, _REAL)
    _fallback_installed = True


def pytest_unconfigure(config: pytest.Config) -> None:
    """Убрать подложенный пример. Живой профиль сюда не попадает: см. `_fallback_installed`."""
    global _fallback_installed
    if _fallback_installed:
        _REAL.unlink(missing_ok=True)
        _fallback_installed = False


@pytest.fixture(scope="session")
def config_imported_at_configure() -> bool:
    """Был ли `hrwork.config` уже импортирован к моменту `pytest_configure` (находка 63)."""
    return _config_imported_at_configure
