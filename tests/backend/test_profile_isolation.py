"""Прогон не зависит от личного `resume_profile.json` запускающего.

Аудит 08.08.2026, находки 57 и 63. Два независимых механизма, и каждый должен быть
ДОКАЗАН, а не заявлен:

* фолбэк профиля стоит в `pytest_configure` корневого conftest, потому что `config.py`
  читает профиль на импорте — иначе один прогон живёт на двух разных профилях (63);
* фикстура `apply_defaults` возвращает `candidates.py` и `cover.py` их дефолтные константы,
  потому что документированные ручки профиля (`blacklists`, `cover_template`) иначе меняют
  результат ~160 тестов отбора и письма (57).

Здесь же контрольные тесты «ручка действительно работает»: без них страж стерёг бы
несуществующий риск и был бы зелёным при любой поломке механизма.
"""
import pytest

from hrwork import config
from hrwork.application.apply import candidates, cover

# Тайтлы-эталоны: на дефолтах оба обязаны отсеиваться (см. test_autoclick.py).
_QA_TITLE = "QA Automation Engineer (Python)"
_SENIOR_TITLE = "Senior Python разработчик"
_DEFAULT_COVER = ("Здравствуйте! Заинтересовала вакансия Data Engineer в компании Ozon. "
                  "Буду рад обсудить детали.")


def test_profile_fallback_precedes_config_import(config_imported_at_configure):
    """`hrwork.config` НЕ импортирован к моменту `pytest_configure` (находка 63).

    Иначе фолбэк профиля опаздывает: на чистом клоне config уже прочитал отсутствующий файл
    и весь прогон живёт на `_RESUME_DEFAULT`, пока движки ответов читают подложенный пример.
    Сломать это можно молча — одной строкой `from hrwork... import ...` на уровне модуля в
    conftest стартового пути (`testpaths = tests/backend`): такие conftest'ы грузятся РАНЬШЕ
    `pytest_configure`."""
    assert config_imported_at_configure is False


@pytest.mark.parametrize("key, expected", [
    ("APPLY_BLACKLISTS", {}),
    ("RESUME_COVER_TEMPLATE", ""),
    ("RESUME_CORE", ["Python", "FastAPI"]),
    ("RESUME_EXP_IDS", ["noExperience", "between1And3"]),
    ("APPLY_CORE_WIDE", {"Django", "Flask", "PostgreSQL", "MySQL", "Redis", "Kafka"}),
    ("APPLY_OFFICE_CITIES", {"Москва", "Санкт-Петербург", "Тольятти", "Самара"}),
    ("APPLY_EXTRA_EXP_IDS", ["between3And6"]),
])
def test_pinned_profile_defaults_are_the_documented_ones(key, expected, pinned_profile_defaults):
    """Чем именно `apply_defaults` подменяет профиль — литералом, а не «тем, что в config».
    Разъедется дефолт в коде — тест назовёт ключ, а не молча переедет вместе с ним."""
    assert pinned_profile_defaults[key] == expected


@pytest.mark.parametrize("pin_key, profile_key", [
    ("RESUME_CORE", "core"),
    ("RESUME_EXP_IDS", "exp_ids"),
])
def test_pinned_resume_defaults_match_the_repo_defaults(pin_key, profile_key,
                                                        pinned_profile_defaults):
    """СТРАЖ СОГЛАСОВАННОСТИ: литералы выше — те же, что в `config._RESUME_DEFAULT`.
    У тиров такой страж уже есть (test_profile_settings.py::test_tier_defaults_unchanged),
    у `core`/`exp_ids` не было."""
    assert pinned_profile_defaults[pin_key] == config._RESUME_DEFAULT[profile_key]


# ── Находка 57: ручка профиля не должна протекать в тесты ──────────────────────────────
# «Проверь делом»: сначала контроль — ручка ДЕЙСТВИТЕЛЬНО меняет скомпилированное правило,
# потом — что `apply_defaults` возвращает дефолт поверх такого профиля.

def test_profile_can_switch_the_qa_rule_off(monkeypatch, module_constants_with_profile):
    """КОНТРОЛЬ. `"blacklists": {"qa": []}` — рабочая документированная ручка: с ней QA
    перестаёт быть вне отбора, и ~60 тестов test_autoclick.py покраснели бы."""
    off = module_constants_with_profile(candidates, {"APPLY_BLACKLISTS": {"qa": []}})
    monkeypatch.setattr(candidates, "APPLY_QA_BLACKLIST", off["APPLY_QA_BLACKLIST"])
    assert candidates.out_of_scope(_QA_TITLE) is None


@pytest.mark.parametrize("title, expected_reason", [
    (_QA_TITLE, "QA"),
    (_SENIOR_TITLE, "senior/lead"),
])
def test_defaults_win_over_a_profile_that_switches_rules_off(title, expected_reason, monkeypatch,
                                                             module_constants_with_profile):
    """Модуль, скомпилированный на профиле «правила мне не нужны», после `apply_defaults`
    отбирает как дефолт. Ожидаемое — подпись причины литералом (`OutOfScope.label`)."""
    off = module_constants_with_profile(candidates, {"APPLY_BLACKLISTS": {"qa": [], "senior": []}})
    for name in ("APPLY_QA_BLACKLIST", "APPLY_SENIOR_BLACKLIST"):
        monkeypatch.setattr(candidates, name, off[name])      # профиль владельца
    for name, value in module_constants_with_profile(candidates).items():
        monkeypatch.setattr(candidates, name, value)          # то же делает apply_defaults
    reason = candidates.out_of_scope(title)
    assert reason is not None and reason.label == expected_reason


def test_profile_can_replace_the_cover_template(monkeypatch, module_constants_with_profile):
    """КОНТРОЛЬ. `cover_template` — рабочая ручка: письмо крон-откликов становится другим."""
    mine = module_constants_with_profile(cover, {"RESUME_COVER_TEMPLATE": "Привет, {name}!"})
    monkeypatch.setattr(cover, "COVER_TEMPLATE", mine["COVER_TEMPLATE"])
    assert cover.template_cover("Data Engineer", "Ozon") == "Привет, Data Engineer!"


def test_default_cover_template_wins_over_a_profile_that_replaces_it(
        monkeypatch, module_constants_with_profile):
    mine = module_constants_with_profile(cover, {"RESUME_COVER_TEMPLATE": "Привет, {name}!"})
    monkeypatch.setattr(cover, "COVER_TEMPLATE", mine["COVER_TEMPLATE"])
    for name, value in module_constants_with_profile(cover).items():
        monkeypatch.setattr(cover, name, value)               # то же делает apply_defaults
    assert cover.template_cover("Data Engineer", "Ozon") == _DEFAULT_COVER
