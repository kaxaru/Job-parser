"""Тесты разбора сырых данных HH в модель Vacancy (чистая логика, без сети)."""
import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.parsing import (
    _detect_role,
    _detect_techs,
    _salary_mid,
    is_hard_non_it,
    parse_vacancy,
)
from hrwork.domain.role import Role
from hrwork.domain.schedule import Schedule

RAW = {
    "id": "123",
    "name": "Python Developer",
    "area": {"id": "1", "name": "Москва"},
    "salary": {"from": 150000, "to": 200000, "currency": "RUR", "gross": False},
    "experience": {"id": "between1And3"},
    "schedule": {"id": "remote"},
    "snippet": {"requirement": "Python, Docker", "responsibility": ""},
}


# ── _salary_mid: середина вилки + gross->net (×0.87) ──
def test_salary_mid_from_and_to():
    assert _salary_mid({"from": 100000, "to": 200000, "currency": "RUR"}) == (100000, 200000, 150000)


def test_salary_mid_gross_to_net():
    # gross=True -> чистыми (×0.87)
    assert _salary_mid({"from": 100000, "to": 200000, "gross": True}) == (87000, 174000, 130500)


def test_salary_mid_only_from():
    assert _salary_mid({"from": 120000}) == (120000, None, 120000)


def test_salary_mid_only_to():
    assert _salary_mid({"to": 90000}) == (None, 90000, 90000)


def test_salary_mid_none_or_empty():
    assert _salary_mid(None) == (None, None, None)
    assert _salary_mid({}) == (None, None, None)


# ── _detect_techs: детект технологий регэкспами ──
def test_detect_techs_finds_python():
    assert "Python" in _detect_techs("Ищем Python-разработчика")


def test_detect_techs_java_not_in_javascript():
    # negative lookahead: Java НЕ матчится внутри "javascript"
    techs = _detect_techs("javascript developer")
    assert "JavaScript" in techs
    assert "Java" not in techs


def test_detect_techs_none_for_neutral_text():
    assert _detect_techs("просто текст без какого-либо стека") == []


def test_detect_techs_csharp_standalone():
    # регресс: '\bc#\b' не матчил «c#» перед пробелом/запятой/концом строки
    # (после '#' граница слова не образуется) — детект держался только на '.net'
    assert "C#" in _detect_techs("знание c#, sql")
    assert "C#" in _detect_techs("стек: c#")


def test_detect_techs_csharp_not_inside_word():
    assert "C#" not in _detect_techs("abc# xyz")


def test_detect_techs_ignores_device_requirement():
    # регресс: «смартфон с ОС iOS или Android» — требование к устройству соискателя,
    # а не стек. Раньше вешало Android/iOS на промоутеров/курьеров/расклейщиков.
    techs = _detect_techs(
        "Представитель Яндекс Поиск (промоутер) Для работы потребуется "
        "Смартфон или планшет с ОС iOS или Android (версия 10 и выше) "
        "Пауэрбанк для подзарядки"
    )
    assert techs == []


def test_detect_techs_keeps_os_for_real_mobile_vacancy():
    # у настоящей мобильной вакансии стек назван и вне device-фразы — тег выживает
    techs = _detect_techs(
        "Android-разработчик. Разработка на Kotlin, Android SDK. "
        "Выдаём тестовый смартфон на Android 14."
    )
    assert "Android" in techs


# ── _detect_role: фолбэк роли ──
def test_detect_role_fallback_needs_language_not_platform():
    # регресс: одиночная платформа/ИИ-упоминание производила не-инженера в «Разработчики»
    assert _detect_role("Мобильный банкир", ["Android", "iOS"]) is Role.NON_IT
    assert _detect_role("Эксперт по обучению ИИ в сфере права", ["ML/AI"]) is Role.NON_IT
    assert _detect_role("Специалист по автоматизации процессов", ["Python"]) is Role.DEVELOPER


def test_detect_role_gig_labeling_is_non_it():
    # крауд-разметка честно матчит ROLE_PATTERNS (AI-тренер -> Data/ML), но это не инженерия
    assert _detect_role("AI-тренер для обучения нейросетей", ["ML/AI"]) is Role.NON_IT
    assert _detect_role("Врач для обучения нейросетей", ["ML/AI"]) is Role.NON_IT
    assert _detect_role("Автотестировщик в крауд-тестирование", ["Python"]) is Role.NON_IT
    assert _detect_role("Асессор", []) is Role.NON_IT
    assert _detect_role("Специалист по разметке данных ИИ", []) is Role.NON_IT


def test_detect_role_gig_rule_keeps_real_ml():
    # правило по СУТИ работы: настоящие ML/QA-инженеры не задеты
    assert _detect_role("ML Engineer", ["Python", "ML/AI"]) is Role.DATA_ML
    assert _detect_role("Инженер по автотестам", ["Python"]) is Role.QA
    assert _detect_role("Специалист по краудфандингу", []) is Role.NON_IT   # НЕ через крауд-правило


def test_detect_role_creative_ai_buzzword_is_non_it():
    # БАГ 21.07: «Продюсер AI видео» -> ROLE_PATTERNS Data/ML матчит «\bai\b» -> роль стала IT,
    # отклик ушёл на не-инженерную роль. Креатив/маркетинг с баззвордом «AI» -> НЕ-IT.
    assert _detect_role("Продюсер AI видео", ["ML/AI"]) is Role.NON_IT
    assert _detect_role("AI-маркетолог", ["ML/AI"]) is Role.NON_IT
    assert _detect_role("Видеограф / монтажёр", []) is Role.NON_IT
    assert _detect_role("SMM-менеджер с нейросетями", ["ML/AI"]) is Role.NON_IT
    # гейт на ЯЗЫК: настоящий dev с «продюсерским» контекстом в тайтле НЕ задет
    assert _detect_role("Python-разработчик в продюсерский центр",
                        ["Python"]) is Role.DEVELOPER


def test_detect_role_creative_desc_language_does_not_rescue():
    # БАГ 22.07: «Продюсер AI-видео» с Python/JS в СНИППЕТЕ прошёл языковой гейт по desc-техам
    # -> роль Data/ML -> отклик ушёл (id 135341881). Язык открывает гейт ТОЛЬКО из тайтла.
    assert _detect_role("Продюсер AI-видео", ["Python", "JavaScript", "ML/AI"]) is Role.NON_IT
    assert _detect_role("AI-маркетолог", ["Python"]) is Role.NON_IT


def test_detect_role_test_engineer_hardware_is_non_it():
    # БАГ 22.07: «Инженер-испытатель» (hardware/стенды) с языком из JD уходил в DEVELOPER-фолбэк
    # -> отклик ушёл (id 135181355). Роль-существительное «испытатель» — безусловный не-IT.
    assert _detect_role("Инженер-испытатель", ["Python"]) is Role.NON_IT
    assert _detect_role("Инженер-испытатель БПЛА", []) is Role.NON_IT
    # «испытательный срок» в тайтле — НЕ триггер
    assert _detect_role("Разработчик Python (испытательный срок 3 мес)",
                        ["Python"]) is Role.DEVELOPER


def test_detect_role_title_wins_over_fallback():
    # тайтл называет роль -> фолбэк не участвует, язык не нужен
    assert _detect_role("Business Analyst (Fintech)", []) is Role.ANALYST
    assert _detect_role("Специалист по тестированию", []) is Role.QA


# АУДИТ 09.08.2026: сорок два случая стояли цепочкой assert в ОДНОМ теле — тот же дефект,
# что и запрещённый docs/testing.md цикл: падение на первом тайтле прятало сорок один
# остальной, а отчёт называл имя теста вместо виновной вакансии. Роль решает, уйдёт ли
# необратимый отклик, поэтому знать надо КАЖДЫЙ разошедшийся тайтл, а не первый.
@pytest.mark.parametrize(("title", "techs", "expected"), [
    # поддержка / преподавание / продажи — не инженерные, даже с Python (Яндекс Крауд и др.)
    ("Специалист поддержки пользователей Yandex Obs", ["Python"], Role.NON_IT),
    ("Специалист технической поддержки", ["Python"], Role.NON_IT),          # слово между
    ("Специалист технической поддержки в службу Дост", ["Python"], Role.NON_IT),
    ("Преподаватель программирования C++", ["Python", "C++"], Role.NON_IT),
    ("Преподаватель-программист", ["Python"], Role.NON_IT),
    ("Репетитор по Python", ["Python"], Role.NON_IT),
    ("Менеджер по продажам (горячие лиды)", ["Python"], Role.NON_IT),
    # академическая подработка: предметная область в тайтле делала её «технической»
    ("Автор студенческих работ по направлению «Математическое моделирование "
     "и численные методы»", ["Python"], Role.NON_IT),
    # образование/методология: учат программированию -> тех-слова в JD, но роль не инженерная
    ("Педагог дополнительного образования (Программирование)", ["Python"], Role.NON_IT),
    ("Практикант IT Методист (написание IT-кейсов и заданий)", ["Python"], Role.NON_IT),
    ("Программист - Тьютор / Наставник Школы Программирования", ["Python"], Role.NON_IT),
    # SEO/маркетинг: скриптуют выдачу -> Python/JS в JD, но роль не инженерная
    ("Automation-first SEO Operator", ["Python"], Role.NON_IT),
    ("Middle SEO-специалист / SEO - оптимизатор", ["Python"], Role.NON_IT),
    ("SEO - специалист (AI)", ["Python"], Role.NON_IT),
    ("Специалист по SEO", ["Python"], Role.NON_IT),
    # контроль: программист, автоматизирующий SEO-отдел — НАСТОЯЩАЯ dev-вакансия
    ("Программист для автоматизации задач SEO-отдела", ["Python"], Role.DEVELOPER),
    # физбезопасность/инженерные системы зданий: роль Security ловила их по слову «безопасн»
    ("Младший инженер отдела эксплуатации систем и средств безопасности",
     ["Python", "PostgreSQL"], Role.NON_IT),
    ("Инженер по эксплуатации систем жизнеобеспечения", ["1С"], Role.NON_IT),
    # финансовый/бизнес-аудит — не инженерная роль
    ("Специалист по цифровым технологиям аудита", ["Python"], Role.NON_IT),
    ("Старший внутренний аудитор", ["Python"], Role.NON_IT),
    ("Аудитор бизнес-процессов", ["Python"], Role.NON_IT),
    # КОНТРОЛЬ: технический аудит остаётся IT
    ("Аудитор смарт-контрактов / Blockchain auditor (Solidity)", ["Python"], Role.DEVELOPER),
    ("Аудитор защищенности приложений (Application Security)", ["Python"], Role.SECURITY),
    ("Аудитор информационной безопасности", ["Python"], Role.SECURITY),
    # юристы/кадры/маркетинг
    ("Младший юрисконсульт", ["Python"], Role.NON_IT),
    ("IT-рекрутер", ["Python"], Role.NON_IT),
    ("Начальник отдела кадров", ["Python"], Role.NON_IT),
    # КОНТРОЛЬ: аналитик в HR-Tech продукте — настоящая IT-роль
    ("Функциональный аналитик в HR-Tech", ["Python"], Role.ANALYST),
    # медиа/маркетинг: «в IT» в тайтле не делает роль инженерной
    ("Рилсмейкер / Монтажер коротких роликов в IT", ["Python"], Role.NON_IT),
    ("Контент-менеджер", ["Python"], Role.NON_IT),
    ("SMM-специалист (gamedev)", ["Python"], Role.NON_IT),
    ("B2B-копирайтер", ["Python"], Role.NON_IT),
    ("Таргетолог", ["Python"], Role.NON_IT),
    # контроль: «монтаж» в промышленном смысле — не медиа-роль
    ("Ведущий инженер-технолог по сборке и электромонтажу", ["Python"], Role.DEVELOPER),
    # добыча/финучёт: доменная роль, а не инженерная
    ("Инженер-аналитик по разработке месторождений", ["Python"], Role.NON_IT),
    ("Аналитик нефтегаза", ["Python"], Role.NON_IT),
    ("Бухгалтер по основным средствам", ["Python"], Role.NON_IT),
    # контроль: IT в добывающей отрасли остаётся IT (целимся в роль, не в отрасль)
    ("Инженер - программист по сопровождению автоматизации бурения", ["Python"], Role.DEVELOPER),
    ("Аналитик данных по направлению Геологоразведка и добыча", ["Python"], Role.ANALYST),
    # контроль: «инженер-наставник» в продуктовой команде — НЕ образование
    ("Backend-разработчик", ["Python"], Role.BACKEND),
    # контроль: настоящий разработчик/QA по тайтлу не задет
    ("Python разработчик", ["Python"], Role.DEVELOPER),
    ("Специалист по тестированию", ["Python"], Role.QA),
])
def test_detect_role_support_teaching_sales_are_non_it(title, techs, expected):
    assert _detect_role(title, techs) is expected


# ── роль: ритейл-продажи всегда Не-IT, даже если прилип тех-тег из текста ──
def test_retail_seller_is_non_it_despite_tech_tag():
    raw = {**RAW, "name": "Продавец-кассир",
           "snippet": {"requirement": "работа с приложением на Android и iOS", "responsibility": ""}}
    v = parse_vacancy(raw)
    assert "Android" in v.techs        # тег реально прилип из текста вакансии
    assert v.role is Role.NON_IT       # но роль — не-IT (жёсткий фильтр по тайтлу)


def test_real_dev_stays_a_mobile_developer():
    # Точный член Role, а не флаг `.is_it` (= «любая из 16 IT-ролей»): роль питает фасет
    # ленты, чипы дашборда и срезы аналитики, и перепутанная роль (Mobile -> QA) была бы
    # незаметна (аудит 09.08.2026). По ROLE_PATTERNS `Mobile` стоит первым (`android`).
    v = parse_vacancy({**RAW, "name": "Android разработчик"})
    assert v.role is Role.MOBILE


# ── is_hard_non_it: отсев на этапе сбора (по тайтлу, до enrich) ──
@pytest.mark.parametrize("name", [
    "Продавец-кассир", "Слесарь-ремонтник", "Машинист крана", "Сметчик",
    "Инженер КИПиА", "Монтажник", "Водитель категории C",
])
def test_is_hard_non_it_blue_collar(name):
    assert is_hard_non_it(name)


@pytest.mark.parametrize("name", [
    "Python-разработчик", "Backend Engineer", "QA Automation",
    "Системный аналитик", "DevOps инженер",
])
def test_is_hard_non_it_keeps_real_it(name):
    assert not is_hard_non_it(name)


# ── parse_vacancy: raw dict -> Vacancy ──
def test_parse_vacancy_basic():
    v = parse_vacancy(RAW)
    assert isinstance(v, Vacancy)
    assert v.id == "123"
    assert v.name == "Python Developer"
    assert v.salary.mid == 175000          # (150000+200000)//2, net (gross=False)
    assert v.experience is Experience.BETWEEN_1_3
    assert v.schedule is Schedule.REMOTE
    assert v.techs == ["Python", "Docker"]


def test_parse_vacancy_city_fallback_to_area():
    assert parse_vacancy(RAW).city == "Москва"   # нет _city -> area.name


def test_parse_vacancy_prefers_underscore_city():
    assert parse_vacancy({**RAW, "_city": "Санкт-Петербург"}).city == "Санкт-Петербург"


def test_parse_vacancy_no_salary():
    v = parse_vacancy({**RAW, "salary": None})
    assert v.salary is None


# ── source: портал-источник (дефолт hh, из _source) ──
def test_parse_vacancy_source_default_hh():
    assert parse_vacancy(RAW).source == "hh"


def test_parse_vacancy_source_from_underscore_key():
    assert parse_vacancy({**RAW, "_source": "hirify"}).source == "hirify"


# ── Детектор ролей: английские инженерные тайтлы (01.08.2026) ───────────────────────────
# getmatch — кураторский IT-портал, поэтому его «Не-IT» = гарантированный промах детектора.
# Замер на 491 карточке: 104 ложных «Не-IT», из них 47 чинят правила ниже. Детектор знал
# русские тайтлы и терял чисто английские ML/инфра/безопасность.

@pytest.mark.parametrize("title,expected", [
    # ML: аббревиатуры, которых не было в словаре
    ("MLE (Online RL) / Post-Training LLM (Middle+ / Senior)", Role.DATA_ML),
    ("Senior DL (VLM, GigaChat Vision)", Role.DATA_ML),
    ("Deep Learning/CUDA Engineer (GigaChat)", Role.DATA_ML),
    ("Senior Research Engineer (LLM Pretraining)", Role.DATA_ML),
    ("Специалист по разработке нейронных сетей", Role.DATA_ML),
    # инфраструктура
    ("System Administrator", Role.DEVOPS),
    ("Windows Server Engineer", Role.DEVOPS),
    ("Senior Database Engineer", Role.DEVOPS),
    ("Senior IaaS / Kubernetes Platform Engineer", Role.DEVOPS),
    # безопасность: infrasec != infosec, а DLP/EDR/СЗИ слова «безопасность» не содержат
    ("Senior InfraSec Engineer", Role.SECURITY),
    ("Penetration Testing Specialist (CICADA8)", Role.SECURITY),
    ("Администратор систем сбора событий с конечных точек (EDR)", Role.SECURITY),
    ("Администратор средств защиты от утечек критической информации (DLP)", Role.SECURITY),
    ("DevSecOps-инженер", Role.SECURITY),
    # управление инженерными командами
    ("Engineering Manager: Offsite Discovery", Role.MANAGER),
    ("Principal Tech Lead в домен Retail", Role.MANAGER),
    ("Старший менеджер продукта (Каналы в MAX)", Role.MANAGER),
    ("Staff Engineer", Role.DEVELOPER),
])
def test_detect_role_english_engineering_titles(title, expected):
    assert _detect_role(title, []) is expected


def test_devsecops_stays_security_not_devops():
    """DevOps проверяется РАНЬШЕ Security: пока `devsecops` стоял в DevOps, 24 вакансии
    с явной «безопасной разработкой» переставали быть Security."""
    assert _detect_role("Инженер по безопасной разработке (DevSecOps)", []) is Role.SECURITY
    assert _detect_role("Application Security инженер (AppSec/DevSecOps)", []) is Role.SECURITY


@pytest.mark.parametrize("title,expected", [
    # IaaS — домен продукта, а не роль: тайтл называет backend-разработчика
    ("Старший Go-разработчик, IaaS", Role.DEVELOPER),
    # голый kubernetes в DevOps крал разработчиков (DevOps идёт раньше в таблице)
    ("Python-разработчик (Kubernetes)", Role.DEVELOPER),
    # `platform engineer` крал у Data Eng
    ("Data Platform Engineer", Role.DATA_ENG),
])
def test_broad_infra_words_do_not_steal_specific_roles(title, expected):
    assert _detect_role(title, ["Python"]) is expected


# ── Поддержка вне выдачи: английские варианты правила (01.08.2026) ──────────────────────
# Решение сохранено прежним (инцидент «Яндекс Крауд: Поддержка»): поддержка — не инженерная
# роль. Правило знало только русские тайтлы, поэтому английские уезжали в IT, а «сопровождение
# 1С» — даже в «Разработчика», т.к. 1С считается языком и открывала фолбэк по стеку.

@pytest.mark.parametrize("title", [
    "IT Support L3 (IT Administrator)",
    "Helpdesk инженер",
    "Инженер сопровождения 1С (L2)",
    "Специалист группы поддержки бизнес-приложений",
    "Главный инженер по сопровождению HR платформы / Консультант SAP",
])
def test_support_titles_are_non_it(title):
    assert _detect_role(title, ["1С"]) is Role.NON_IT


@pytest.mark.parametrize("title", [
    "Разработчик 1С / Сопровождение 1С (доработка, конфигурирование)",
    "Инженер-программист по сопровождению 1С",
    "Старший программист отдела сопровождения 1С",
])
def test_support_gate_spares_titles_that_name_a_developer(title):
    """«Сопровождение» часто дописывают к обязанностям настоящего разработчика — прятать
    такие тайтлы нельзя. Замер: паттерн ловит 477 вакансий, названы разработчиком 8."""
    # Точная роль, а не отрицание `is not NON_IT` (проходило на 16 разных ответах):
    # «сопровождение» рядом с «разработчик/программист» — это ключ `Разработчик`
    # в ROLE_PATTERNS, и от роли зависит и фасет ленты, и отбор под отклик.
    assert _detect_role(title, ["1С"]) is Role.DEVELOPER
