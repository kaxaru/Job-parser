"""Общая подготовка backend-тестов: отбор и письмо считаются на ДЕФОЛТАХ проекта.

Фолбэк профиля отсюда УЕХАЛ в корневой `conftest.py::pytest_configure` (находка 63 аудита
08.08.2026): `hrwork/config.py` читает `resume_profile.json` на импорте, и фикстура,
работавшая при setup первого теста, опаздывала на целую конфигурацию.

Здесь закрыта вторая половина той же проблемы (находка 57). `candidates.py` компилирует
чёрные списки, а `cover.py` — шаблон письма ОДИН РАЗ на импорте, из значений профиля.
Владелец, воспользовавшийся ДОКУМЕНТИРОВАННОЙ ручкой (`"blacklists": {"qa": []}`,
`"cover_template": "..."`), получал либо сотню красных тестов, либо — хуже — зелёные тесты,
проверяющие его личные предпочтения вместо дефолтов проекта. `apply_defaults` возвращает
модулям их дефолтные константы, поэтому прогон на чужой машине с чужим профилем даёт тот же
результат.

ВАЖНО: ни одного импорта `hrwork` на уровне модуля. Этот conftest относится к стартовому
пути (`testpaths = tests/backend`) и грузится РАНЬШЕ `pytest_configure` корневого conftest —
импорт `hrwork.config` отсюда испёк бы конфигурацию до подкладывания профиля и вернул бы
находку 63. Инвариант стережёт test_profile_isolation.py.
"""
import importlib.util
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest

_DEFAULTS_CACHE: dict[str, dict[str, Any]] = {}


def _default_profile() -> dict[str, Any]:
    """Профиль, свёрнутый в ДЕФОЛТЫ: «в resume_profile.json такого ключа нет».

    `core`/`exp_ids` записаны литералом (страж —
    test_profile_isolation.py::test_pinned_resume_defaults_match_the_repo_defaults), остальные
    берутся у config: их неизменность уже стережёт
    test_profile_settings.py::test_tier_defaults_unchanged."""
    from hrwork import config
    return {
        "APPLY_BLACKLISTS": {},                 # ни одно правило отбора не переопределено
        "RESUME_COVER_TEMPLATE": "",            # своего письма нет -> дефолтный шаблон cover.py
        "RESUME_CORE": ["Python", "FastAPI"],
        "RESUME_EXP_IDS": ["noExperience", "between1And3"],
        "APPLY_CORE_WIDE": set(config._CORE_WIDE_DEFAULT),
        "APPLY_OFFICE_CITIES": set(config._OFFICE_CITIES_DEFAULT),
        "APPLY_EXTRA_EXP_IDS": list(config._EXTRA_EXP_IDS_DEFAULT),
    }


def _module_constants(module: ModuleType, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """UPPER-константы модуля, пересчитанные на ЗАДАННОМ профиле (None -> дефолты).

    Иначе дефолты не достать: они стоят прямо в вызовах `candidates.py::_rx(key, default)`
    и после импорта уже перекрыты профилем запускающего, а из скомпилированного
    `re.Pattern` исходный дефолт не восстановить.

    Модуль выполняется заново в отдельном пространстве имён и в `sys.modules` НЕ попадает.
    Наружу отдаются только значения констант: классы второго экземпляра (`OutOfScope`,
    `ApplyTier`) остались бы не равны настоящим по `is`, поэтому фильтр по UPPER-именам."""
    from hrwork import config
    values = {**_default_profile(), **(profile or {})}
    spec = importlib.util.spec_from_file_location(f"{module.__name__}__defaults", module.__file__)
    if spec is None or spec.loader is None:                  # модуль не из файла — не наш случай
        raise RuntimeError(f"{module.__name__}: модуль не перечитывается из файла")
    fresh = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as mp:
        for name, value in values.items():
            mp.setattr(config, name, value)                  # raising=True: опечатка в ключе упадёт
        spec.loader.exec_module(fresh)
    return {n: v for n, v in vars(fresh).items() if n.isupper() and not n.startswith("_")}


def _cached_defaults(module: ModuleType) -> dict[str, Any]:
    """`_module_constants` на дефолтах, посчитанный один раз за сессию (перечитывание модуля
    стоит компиляции десятка регексов, а тестов отбора больше полутора сотен)."""
    if module.__name__ not in _DEFAULTS_CACHE:
        _DEFAULTS_CACHE[module.__name__] = _module_constants(module)
    return _DEFAULTS_CACHE[module.__name__]


@pytest.fixture(scope="session")
def module_constants_with_profile() -> Callable[..., dict[str, Any]]:
    """Фабрика для стражей изоляции: константы модуля, скомпилированные на заданном профиле."""
    return _module_constants


@pytest.fixture(scope="session")
def pinned_profile_defaults() -> dict[str, Any]:
    """Значения профиля, на которые `apply_defaults` прибивает модули отклика."""
    return _default_profile()


@pytest.fixture
def apply_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прибить отбор (`candidates.py`) и письмо (`cover.py`) к ДЕФОЛТАМ проекта.

    Подключается модульным `pytestmark = pytest.mark.usefixtures("apply_defaults")` там, где
    тест обязан проверять дефолтное поведение: test_autoclick.py, test_cover.py."""
    from hrwork.application.apply import candidates, cover
    for module in (candidates, cover):
        for name, value in _cached_defaults(module).items():
            monkeypatch.setattr(module, name, value)
