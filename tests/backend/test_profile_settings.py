"""Личные настройки живут в `resume_profile.json`, а не в исходниках.

Владелец форка должен настраивать систему под себя правкой ОДНОГО файла: его правки в
`config.py` конфликтовали бы при каждом обновлении из апстрима. До 08.08.2026 в коде
буквально стояли город владельца (`APPLY_OFFICE_CITIES`), его поисковые запросы и тексты
писем ленты.

Механизм `_profile_list` читает уже загруженный `config._rp` (профиль читается на импорте
модуля), поэтому здесь подменяется словарь, а не файл на диске.
"""
import json
from pathlib import Path

import pytest

from hrwork import config as C
from hrwork.application.apply import candidates as CND

_EXAMPLE = Path(__file__).parents[2] / "resume_profile.example.json"


@pytest.fixture
def profile(monkeypatch):
    """Подменить загруженный профиль на время теста."""
    def _set(mapping):
        monkeypatch.setattr(C, "_rp", mapping)
        return mapping
    return _set


def test_key_absent_uses_the_default(profile):
    profile({})
    assert C._profile_list("office_cities", ["Москва"]) == ["Москва"]


def test_profile_value_wins_over_the_default(profile):
    profile({"office_cities": ["Казань", "Уфа"]})
    assert C._profile_list("office_cities", ["Москва"]) == ["Казань", "Уфа"]


def test_empty_list_disables_the_setting(profile):
    """Пустой список = «эта настройка мне не нужна», и в дефолт он НЕ проваливается.
    Тот же контракт, что у `blacklists` с пустой строкой. Иначе «офис мне не нужен»
    (`"office_cities": []`) молча вернуло бы город прошлого владельца профиля."""
    profile({"office_cities": []})
    assert C._profile_list("office_cities", ["Москва"]) == []


def test_values_are_coerced_to_strings(profile):
    # area id в JSON человек может написать числом — сравнение с городом вакансии строковое
    profile({"office_cities": [1, "Москва"]})
    assert C._profile_list("office_cities", []) == ["1", "Москва"]


def test_wrong_type_falls_back_to_the_default(profile):
    """Профиль пишет человек: строка вместо списка это опечатка, а не настройка.
    Без проверки типа `[str(x) for x in "Москва"]` дал бы список букв."""
    profile({"office_cities": "Москва"})
    assert C._profile_list("office_cities", ["Самара"]) == ["Самара"]


def test_empty_list_falls_back_when_emptiness_is_meaningless(profile):
    """Второй контракт, выбираемый явно: для `search_queries` пустой список — не «настройка
    снята», а сломанный конфиг, который обнулил бы сбор целиком."""
    profile({"search_queries": []})
    assert C._profile_list_or_default("search_queries", ["python"]) == ["python"]


def test_explicit_values_win_in_both_contracts(profile):
    profile({"search_queries": ["go разработчик"]})
    assert C._profile_list_or_default("search_queries", ["python"]) == ["go разработчик"]


# ═══ Коды опыта из профиля: опечатка не молчит ════════════════════════════════════════
# РЕГРЕССИЯ 08.08.2026. Раньше опечатку в `exp_ids` выдавал KeyError при сборке ленты
# (подписи брались по коду), но после удаления моста RESUME_EXPS_PY она перестала
# проявляться где-либо: неизвестный код просто ни с чем не совпадал и молча сужал и процент
# матча в ленте, и отбор под отклик. Контракт файла профиля — предупреждение, а не падение
# (`_profile_list`): его пишет человек, и config импортируется каждой командой.
@pytest.fixture
def log_lines():
    """Строки, ушедшие в лог за тест. Свой сток, потому что loguru не пишет в `caplog`."""
    lines: list[str] = []
    sink = C.log.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    yield lines
    C.log.remove(sink)


def test_unknown_experience_code_is_dropped_and_named_in_the_log(log_lines):
    assert C._known_exp_ids("exp_ids", ["noExperience", "betwen1And3"]) == ["noExperience"]
    assert log_lines == [
        "resume_profile.exp_ids: неизвестный код опыта ['betwen1And3'] — допустимы "
        "['between1And3', 'between3And6', 'moreThan6', 'noExperience']; "
        "код исключён из отбора"]


def test_known_experience_codes_pass_through_silently(log_lines):
    assert C._known_exp_ids("extra_exp_ids", ["between3And6"]) == ["between3And6"]
    assert log_lines == []


@pytest.mark.parametrize("name", ["RESUME_EXP_IDS", "APPLY_EXTRA_EXP_IDS"])
def test_live_config_exposes_only_known_experience_codes(name):
    """Оба списка приходят из одного профиля одним способом, поэтому и проверяются оба."""
    assert [e for e in getattr(C, name) if e not in C.EXP_LABELS] == []


def test_repo_defaults_for_search_are_intact():
    """Дефолты сбора после выноса в профиль не сдвинулись. Сверяется САМ дефолт, а не
    действующее значение: у форка с ключами в профиле оно законно другое."""
    assert len(C._SEARCH_QUERIES_DEFAULT) == 35
    assert len(C._CITIES_DEFAULT) == 34
    assert C._CITIES_DEFAULT["1"] == "Москва"


@pytest.mark.parametrize("actual, expected", [
    ("_CORE_WIDE_DEFAULT", ["Django", "Flask", "PostgreSQL", "MySQL", "Redis", "Kafka"]),
    ("_OFFICE_CITIES_DEFAULT", ["Москва", "Санкт-Петербург", "Тольятти", "Самара"]),
    ("_EXTRA_EXP_IDS_DEFAULT", ["between3And6"]),
])
def test_tier_defaults_unchanged(actual, expected):
    """Дефолты тиров — прежнее поведение владельца репозитория: вынос в профиль не должен
    был ничего сдвинуть у того, кто этих ключей не писал. Сверяется САМ дефолт, а не
    действующее значение: у форка с ключами в профиле оно законно другое."""
    assert getattr(C, actual) == expected


def test_example_profile_is_valid_json():
    """Пример — то, что скопирует следующий пользователь. Битый JSON там означает, что
    система молча уедет на дефолты: config глотает JSONDecodeError."""
    json.loads(_EXAMPLE.read_text(encoding="utf-8"))


def test_every_example_key_is_actually_read():
    """Каждый ключ примера обязан кем-то читаться, иначе документация обещает ручку,
    которой нет. Так в примере жил `answers.form_answers`: движок читает form_answers
    ТОЛЬКО с верхнего уровня, и вложенная копия не делала ничего (найдено 08.08.2026)."""
    example = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    keys = {k for k in example if not k.startswith("_")}
    wired = {
        "core", "exp_ids",              # config: RESUME_CORE / RESUME_EXP_IDS
        "answers",                      # chat_answer.py, form_fill.py::_age
        "form_answers",                 # form_fill.py::form_answers
        "blacklists",                   # config: APPLY_BLACKLISTS -> candidates._rx
        "cover_template",               # cover.py (крон-отклики)
        "feed_cover_templates",         # feed.py -> FEED_COVER_TEMPLATES_PY -> cover.js
        "core_wide", "office_cities",   # config: тиры отбора 2 и 3
        "extra_exp_ids",                # config: APPLY_EXTRA_EXP_IDS
        "search_queries", "cities",     # config: что и где собираем на hh
    }
    assert keys == wired, f"в примере лишние/недостающие ключи: {sorted(keys ^ wired)}"


# ═══ Страж: пример отсеивает то же, что и дефолт ═══════════════════════════════════════
# ДРЕЙФ 08.08.2026: в дефолтах `candidates.py` были `\bqc\b` и `test\w*\s+engineer`, а
# в `blacklists.qa` примера — нет. `test_autoclick.py` фиксировал «Test Engineer» как отсев,
# но гонял ДЕФОЛТЫ, а страж примера проверял только компиляцию регексов. Форкер копировал
# пример — и «Test Engineer» / «QC» уходили в автоотклик при полностью зелёных тестах.
#
# Жанр — страж согласованности (docs/testing.md): сравниваются два места, и одна сторона
# (список тайтлов) записана литералом. `_rx` вызывается напрямую сознательно: публичного
# способа собрать ОДНО правило из чужого профиля нет, а `out_of_scope` компилируется на
# импорте и профиль на лету не подхватит.
_EXAMPLE_BLACKLISTS = json.loads(_EXAMPLE.read_text(encoding="utf-8"))["blacklists"]

_DEFAULT_RULE = {                       # правило -> имя дефолтного регекса в candidates.py
    "senior": "APPLY_SENIOR_BLACKLIST",
    "internship": "APPLY_INTERNSHIP_BLACKLIST",
    "management": "APPLY_MANAGEMENT_BLACKLIST",
    "non_engineering": "APPLY_ROLE_BLACKLIST",
    "qa": "APPLY_QA_BLACKLIST",
    "analyst": "APPLY_ANALYST_BLACKLIST",
    "ml": "APPLY_ML_BLACKLIST",
    "devops": "APPLY_DEVOPS_BLACKLIST",
    "other_lang": "APPLY_LANG_BLACKLIST",
    "target_engineering": "APPLY_TARGET_ENGINEERING",
}

_CANON_TITLES = [
    ("senior", "Senior Python Developer"),
    # ДРЕЙФ 08.08.2026 (закрыт): «Sr.» и «принципал» знал только словарь грейдов
    # `domain/grade.py::_TITLE_RX`, а дефолт `candidates.py` — нет. «Sr. Python Developer»
    # проходил отбор как рядовая вакансия, и в том же прогоне считался senior при ответе
    # про деньги. Теперь оба слова есть и в дефолте, и в примере.
    ("senior", "Sr. Python Developer"),
    ("senior", "Принципал-инженер"),
    ("senior", "Сеньор Python-разработчик"),
    ("senior", "Синьор бэкенд-разработчик"),
    ("senior", "Ведущий инженер-программист"),
    ("senior", "Старший разработчик Python"),
    ("senior", "Тимлид команды бэкенда"),
    ("senior", "Team Lead (Python)"),
    ("senior", "Lead Backend Engineer"),
    ("senior", "Технический лид"),
    ("senior", "Principal Engineer"),
    ("senior", "Staff Engineer"),
    # Стажировки отсекаются с 08.08.2026, junior — НЕТ (см. keep-набор ниже).
    ("internship", "Python Developer Intern"),
    ("internship", "Internship: Backend (Python)"),
    ("internship", "Trainee Software Engineer"),
    ("internship", "Стажёр-разработчик Python"),
    ("internship", "Стажер бэкенд-разработки"),
    ("internship", "Стажировка в команду бэкенда"),
    ("internship", "Практикант-программист"),
    ("management", "Руководитель отдела разработки"),
    ("management", "Начальник ИТ-управления"),
    ("management", "Директор по разработке"),
    ("management", "Заведующий лабораторией"),
    ("management", "Заместитель технического директора"),
    ("management", "Head of Engineering"),
    ("management", "Director of Product"),
    ("management", "VP of Engineering"),
    ("management", "Chief Technology Officer"),
    ("management", "CTO стартапа"),
    ("management", "Engineering Manager"),
    ("management", "Delivery Supervisor"),
    ("management", "Solution Architect"),
    ("management", "Архитектор ПО"),
    ("management", "Founder & CEO"),
    ("non_engineering", "Риск-аналитик (проект ПВР)"),
    ("non_engineering", "Риск-менеджер"),
    ("non_engineering", "Портфельный аналитик"),
    ("qa", "QA Engineer"),
    ("qa", "AQA Automation"),
    ("qa", "QC Specialist"),                        # дрейф 08.08.2026: в примере не было
    ("qa", "SDET"),
    ("qa", "Test Engineer"),                        # дрейф 08.08.2026: в примере не было
    ("qa", "Testing Engineer"),
    ("qa", "Quality Assurance Lead"),
    ("qa", "Тестировщик ПО"),
    ("qa", "Инженер по тестированию"),
    ("qa", "Разработчик автотестов"),
    ("analyst", "Системный аналитик"),
    ("analyst", "Business Analyst"),
    ("analyst", "BI-разработчик"),
    ("ml", "ML-инженер"),
    ("ml", "MLOps Engineer"),
    ("ml", "ML-Ops инженер"),
    ("ml", "MLE"),
    ("ml", "Machine Learning Engineer"),
    ("ml", "Инженер машинного обучения"),
    ("ml", "Data Scientist"),
    ("ml", "Дата-сайентист"),
    ("ml", "Deep Learning Researcher"),
    ("ml", "NLP Engineer"),
    ("ml", "Computer Vision Engineer"),
    ("devops", "DevOps Engineer"),
    ("devops", "SRE"),
    ("devops", "DevSecOps инженер"),
    ("devops", "Site Reliability Engineer"),
    ("devops", "Platform Engineer"),
    ("devops", "Системный администратор"),
    ("devops", "Сисадмин"),
    ("devops", "Sysadmin (Linux)"),
    ("devops", "Инженер инфраструктуры"),
    ("other_lang", "Java-разработчик"),
    ("other_lang", "C# Developer"),
    ("other_lang", "C++ Engineer"),
    ("other_lang", "PHP-программист"),
    ("other_lang", "Golang Developer"),
    ("other_lang", "Go-разработчик"),
    ("other_lang", "Scala Engineer"),
    ("other_lang", "Kotlin Developer"),
    ("other_lang", "Rust Developer"),
    ("other_lang", "Delphi программист"),
    ("other_lang", "Perl Developer"),
    ("other_lang", "Ruby on Rails Developer"),
    ("other_lang", ".NET разработчик"),
    ("other_lang", "1С-программист"),
    ("target_engineering", "LLM Engineer"),
    ("target_engineering", "RAG-разработчик"),
    ("target_engineering", "Data Engineer"),
    ("target_engineering", "Дата-инженер"),
    ("target_engineering", "Инженер данных"),
    ("target_engineering", "DWH Developer"),
    ("target_engineering", "ETL Developer"),
]


@pytest.mark.parametrize("key, title", _CANON_TITLES)
def test_example_blacklist_rejects_the_same_titles_as_the_default(monkeypatch, key, title):
    """Правило ПРИМЕРА срабатывает на том же каноническом тайтле, что и дефолт в коде.

    Форк копирует пример и остаётся с ЕГО правилами: расхождение здесь означает, что тесты
    зелёные, а у пользователя отбор другой."""
    assert bool(getattr(CND, _DEFAULT_RULE[key]).search(title)) is True, "дефолт не ловит тайтл"
    monkeypatch.setattr(CND, "APPLY_BLACKLISTS", _EXAMPLE_BLACKLISTS)
    # `(?!)` как дефолт: пропавший в примере ключ должен упасть, а не молча взять наш регекс
    assert bool(CND._rx(key, r"(?!)").search(title)) is True, "правило примера не ловит тайтл"


@pytest.mark.parametrize("title", [
    "Python-разработчик",
    "Backend Developer (Python)",
    "Fullstack-разработчик (Python, React)",
    "JavaScript-разработчик",           # lookaround (?!script) в other_lang примера
    "LLM Engineer",
    "Дата-инженер (ETL)",
    # junior — целевой грейд, правило про стажировки его задевать НЕ должно
    "Junior Python Developer",
    "Джуниор бэкенд-разработчик",
    # «intern» стоит целым словом: соседние слова с той же основой не ловятся
    "Internal Tools Developer (Python)",
    "International Payments Backend Engineer",
])
def test_example_blacklists_keep_the_target_titles(monkeypatch, title):
    """Целевые вакансии пример НЕ отсеивает — иначе паритет достигался бы запретом всего."""
    monkeypatch.setattr(CND, "APPLY_BLACKLISTS", _EXAMPLE_BLACKLISTS)
    hits = [k for k in _DEFAULT_RULE
            if k != "target_engineering" and CND._rx(k, r"(?!)").search(title)]
    assert hits == []


def test_example_answers_keys_match_what_the_engine_reads():
    """То же для вложенного блока `answers`, и в ОБЕ стороны (равенство множеств).

    Слева — то, что увидит следующий пользователь, справа — то, что читает движок ответов.
    Расхождение бывает обеих мастей и обе молчаливые: мёртвый ключ в примере
    (`answers.form_answers`) и ключ, который код читает, а пример не показывает
    (`years_frontend_text` — зонтичный ответ про фронт; оба найдены 08.08.2026)."""
    example = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    keys = {k for k in example["answers"] if not k.startswith("_")}
    # chat_answer.py::_fact / _past_stack / _answer_frontend, form_fill.py::_CTX_KEYS и _age
    wired = {
        "stack", "stack_past", "years_text", "years_python_text", "years_frontend_text",
        "format_text", "english_text", "education_text", "citizenship_text",
        "salary_by_grade", "office_city", "practices", "answer_negative", "birth_date",
        "tech_synonyms",                # chat_answer.py::_spelling_groups (дополняет _TECH_SYNONYMS)
        # английские версии фактов: `_fact` для en ищет <key>_en, нет перевода -> молчим
        "office_city_en", "years_text_en", "years_python_text_en", "years_frontend_text_en",
        "format_text_en", "english_text_en", "education_text_en", "citizenship_text_en",
    }
    assert keys == wired, f"в примере лишние/недостающие ключи: {sorted(keys ^ wired)}"
