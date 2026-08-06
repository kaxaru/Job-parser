"""Отбор вакансий под автоотклик (чистая логика, без браузера — тестируется напрямую).

Профиль отклика = config.RESUME_* (тот же, что в resume.js ленты). Фильтр по стеку/опыту/
удалёнке/свежести + чёрные списки по тайтлу. Вынесено из autoclick (god-модуль): здесь нет
Playwright, поэтому логику можно менять и тестировать без риска для реальных откликов."""
import math
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from hrwork.config import (
    APPLY_CORE_WIDE,
    APPLY_OFFICE_CITIES,
    RESUME_CORE,
    RESUME_EXP_IDS,
)
from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.parsing import has_remote
from hrwork.domain.role import Role


class ApplyTier(IntEnum):
    """Уровень отбора под отклик (VO; раньше — голый int с 0-сентинелом «не подходит»).
    IntEnum: значение = приоритет очереди (меньше = раньше), сортировка/сравнение с int
    работают как были. «Не подходит» — None из _apply_tier, не магический 0."""
    STRICT = 1     # строгое ядро (Python/FastAPI), удалёнка
    WIDE = 2       # широкий стек (Django/Flask/SQL/очереди), удалёнка
    OFFICE = 3     # любой из стеков, офис в APPLY_OFFICE_CITIES


@dataclass
class Candidate:
    """Вакансия, отобранная под отклик — раньше был dict {id,name,url,...} (data clump).
    Поля сверх id/name/url опциональны: отклик из ленты (`_apply_one_vacancy`) знает лишь
    первые три, крон-пул (`pick_candidates`) заполняет всё."""
    id: str
    name: str
    url: str
    employer: str = ""
    exp: Experience | None = None   # уровень опыта (VO; приоритет очереди)
    age: int | None = None   # возраст в днях (приоритет: свежее — раньше)
    desc: str = ""           # текст сниппета (для письма)
    tier: ApplyTier | None = None   # уровень отбора; None — прямой отклик (лента), тир неизвестен

# ── Профиль отклика (единый с resume.js) ──
APPLY_CORE        = set(RESUME_CORE)                     # tier1: строгое ядро (Python/FastAPI)
# Допустимый опыт как VO (config хранит raw id — граница resume_profile.json).
#
# Отклики берут грейд ШИРЕ, чем резюме-матч, и это сознательное расхождение. Профиль резюме
# (`RESUME_EXP_IDS`) описывает реальный опыт и кормит процент совпадения в ленте — задирать
# его нельзя, иначе матч начнёт врать. А отбор под отклик — это «куда имеет смысл писать»:
# вилку «3–6 лет» массово ставят на мидл-позиции, куда откликаться уместно.
#
# Замер 27.07: с одними junior-грейдами в пуле оставалось 296 кандидатов — меньше пяти дней
# работы при темпе ~62 отклика/сутки; с «3–6 лет» пул 1534. Порядок очереди не меняется:
# `pick_candidates` сортирует по tier, а внутри — по грейду (младшие раньше, см. _EXP_ORDER),
# поэтому старшая вилка разбирается последней и только когда junior-кандидаты кончились.
APPLY_EXTRA_EXPS  = {"between3And6"}
APPLY_EXPS        = {e for x in (*RESUME_EXP_IDS, *APPLY_EXTRA_EXPS)
                     if (e := Experience.from_code(x))}
APPLY_SKIP_GHOSTS = True                                 # не откликаться на гост-вакансии (>60 дн)
# ВРЕМЕННО: не пытаться откликаться на вакансии-опросники повторно. Бот их не заполняет
# (вопросы работодателя специфичны), но раньше они НЕ исключались из выборки — очередь
# упиралась в них каждый прогон (19.07: 21 опросник из 56 кандидатов, отклик не двигался).
# Исключаем по форм-очереди, а НЕ через marks: в ленте они остаются с бейджем «форма»,
# отказом не помечаются, и снять фильтр можно одним флагом.
APPLY_SKIP_FORMS = True
# Чёрный список по ГРЕЙДУ: senior-роли и рус-эквиваленты (ведущий/старший/тимлид/lead —
# иначе «Ведущий backend» проскакивает мимо «senior»).
APPLY_SENIOR_BLACKLIST = re.compile(
    r"\bsenior\b|\bсеньор|\bсиньор"
    r"|\bведущ|\bстарш|\bтимлид|\bteam.?lead\b|\blead\b|\bлид\b|\bprincipal\b|\bstaff\b",
    re.I)

# ── Целевая специализация: backend + data/LLM engineering ──────────────────────────────
# Задана пользователем 07.08.2026. Всё, что в неё не входит, из откликов исключается:
# QA, аналитики (любые — и data/BI, и системные/бизнес/продуктовые), ML/DS.
#
# Исключение для ГИБРИДОВ: «Data Engineer/Data Analyst», «Data engineer+analyst (DWH)» —
# инженерные вакансии с аналитическим уклоном, и слово «analyst» рядом не повод их терять.
# Замер на живом пуле: таких 16.
#
# На QA исключение НЕ распространяется (проверка QA идёт раньше): «QA Engineer (LLM-платформа)»
# и «Тестировщик (LLM, ML)» — это тестирование, а не LLM-инженерия, и они должны отсекаться.
# Маркеры описывают РОЛЬ, а не продукт работодателя: «платформа данных» намеренно НЕ входит,
# иначе «Data-аналитик (Платформа данных для финансовой отчётности)» проходил бы как инженер.
APPLY_TARGET_ENGINEERING = re.compile(
    r"\bllm\b|\brag\b|data\s+engineer|дата.?инженер|инженер\w*\s+данных|\bdwh\b|\betl\b",
    re.I)
# QA целиком и БЕЗУСЛОВНО, включая QA с Python в тайтле (в пуле их 10 из 114).
APPLY_QA_BLACKLIST = re.compile(
    r"\bqa\b|\baqa\b|\bqc\b|\bsdet\b|quality\s+assurance|test\w*\s+engineer"
    r"|тестировщ|тестирован|автотест",
    re.I)
# Аналитики целиком. Прежняя логика была ОБРАТНОЙ (APPLY_ANALYST_OK пропускал айтишные
# подтипы) — отменена сознательно: аналитика не входит в целевую специализацию.
APPLY_ANALYST_BLACKLIST = re.compile(r"аналитик|analyst|\bbi\b", re.I)
# ML/DS. LLM и RAG сюда НЕ входят — они целевые, см. APPLY_TARGET_ENGINEERING выше:
# «Data Scientist (NLP / LLM)» остаётся в пуле по маркеру LLM.
# \bml\b не ловит HTML/XML (нет границы перед ml); ловит «ML», «ML-инженер», «ML/DS».
APPLY_ML_BLACKLIST = re.compile(
    r"\bml\b|\bml[\s\-/]?ops\b|\bmlops\b|\bmle\b|machine\s+learning|машинн\w*\s+обучени"
    r"|data\s+scientist|дата.?са[йи]ентист|deep\s+learning|computer\s+vision|\bnlp\b",
    re.I)
# «Ищу только python»: даже если Python есть в требованиях (как «плюс»), вакансию с
# ДРУГИМ основным языком в ТАЙТЛЕ не берём — иначе «Java-разработчик (+Python)» проскочит.
# Только backend-конкуренты Python; JS/TS/React не считаем (Python-fullstack ок).
APPLY_LANG_BLACKLIST = re.compile(
    r"\bjava\b(?!script)|\bc#|\bc\+\+|\bcpp\b|\bphp\b|\bgolang\b|\bscala\b|\bruby\b"
    r"|\bkotlin\b|\brust\b|\bdelphi\b|\bperl\b|\.net\b|\b1с\b|\b1c\b|\bgo[-\s]?разраб",
    re.I)
# НЕ-ИНЖЕНЕРНЫЕ роли, формально проходящие как IT: `_apply_tier` смотрит только на СТЕК и
# формат, поэтому «Риск-аналитик» с упоминанием Python в JD получал tier STRICT. Роль
# (`Аналитик`) тут не помогает — она таксономия РЫНКА для дашборда, а это предпочтения
# ОТКЛИКА, поэтому фильтр живёт в apply-слое.
# Целимся в РОЛЬ-существительное (аналитик/менеджер), а не в домен: «Разработчик моделей
# оценки кредитных рисков» — настоящая dev-вакансия и проходить обязана.
APPLY_ROLE_BLACKLIST = re.compile(
    r"риск.?(?:аналит|менеджер)"                  # Риск-аналитик / Риск-менеджер по аналитике
    r"|портфельн\w*\s*(?:аналит|менеджер)"        # Портфельный аналитик/портфельный менеджер
    r"|планировани\w*.{0,25}ресурс",              # Аналитик по планированию и управлению ресурсами
    re.I)
# Приоритет опыта в очереди: младший грейд раньше (= порядок членов Experience:
# NONE -> BETWEEN_1_3 -> ...). Меньше ранг = раньше. None/неизвестный -> в конец.
_EXP_ORDER = {e: i for i, e in enumerate(Experience)}
_RANK_UNKNOWN = len(_EXP_ORDER)   # опыт не распознан -> после всех известных
_AGE_UNKNOWN = math.inf           # тайминга нет -> в конец очереди по свежести


def _apply_tier(v: Vacancy, is_remote_any: bool) -> ApplyTier | None:
    """Уровень отбора вакансии (None = не подходит ни под один). Приоритет — см. ApplyTier.
    Так пул сортируется по tier: строгие уходят первыми, потом шире, потом офис — без простоя.
    Гибрид (flexible) без текстовых маркеров удалёнки НЕ считается remote -> идёт по офисной
    ветке (только 4 города): гибрид требует ходить в офис — это осознанная семантика."""
    techs = set(v.techs)
    strict = bool(techs & APPLY_CORE)
    wide = bool(techs & APPLY_CORE_WIDE)
    if is_remote_any:
        if strict:
            return ApplyTier.STRICT
        if wide:
            return ApplyTier.WIDE
        return None                                  # удалёнка, но стек не наш
    if (strict or wide) and v.city in APPLY_OFFICE_CITIES:
        return ApplyTier.OFFICE                      # офис — только в разрешённых городах
    return None


def pick_candidates(records: list[Any], marks: dict[str, str], limit: int,
                    form_ids: set[str] | frozenset[str] = frozenset()) -> list["Candidate"]:
    """Вакансии под отклик (list[VacancyRecord]), МНОГОУРОВНЕВО (см. _apply_tier): опыт <3 лет,
    НЕ гост (>60 дн), НЕ в чёрном списке (ML/MLOps/senior по тайтлу), не другой язык в тайтле;
    уже отмеченные — мимо. Приоритет: tier (строгий -> широкий -> офис) -> младший опыт ->
    свежее. Чистая (без браузера). Тайминга нет (старый кеш) -> НЕ отсеиваем по свежести.

    form_ids — id вакансий-опросников (form_vacancies.json): их пропускаем, см. APPLY_SKIP_FORMS."""
    out: list[Candidate] = []
    for rec in records:
        v = rec.vacancy
        if v.source != "hh":
            continue                                    # автоклик Playwright — только HH
        if v.id in marks:
            continue
        if APPLY_SKIP_FORMS and v.id in form_ids:
            continue                                    # опросник — бот его не заполнит, очередь не тратим
        if not v.role.is_it:
            continue                                    # не-IT (поддержка/ритейл/крауд-разметка) — мимо
        if v.experience not in APPLY_EXPS:
            continue                                    # None (не указан) тоже не проходит
        if APPLY_SENIOR_BLACKLIST.search(v.name):
            continue                                    # senior/lead — не наш грейд
        if APPLY_ROLE_BLACKLIST.search(v.name):
            continue                                    # риск/портфельный/планирование ресурсов — не инженерная роль
        # QA — безусловно, до исключения: «QA Engineer (LLM-платформа)» это тестирование.
        if APPLY_QA_BLACKLIST.search(v.name) or v.role is Role.QA:
            continue
        # Аналитика и ML — с исключением: инженерный маркер в тайтле перевешивает.
        if not APPLY_TARGET_ENGINEERING.search(v.name):
            if APPLY_ANALYST_BLACKLIST.search(v.name) or APPLY_ML_BLACKLIST.search(v.name):
                continue                                # аналитик / ML — мимо
            if v.role is Role.ANALYST:
                continue                                # роль поймала то, чего нет в тайтле
        if "python" not in v.name.lower() and APPLY_LANG_BLACKLIST.search(v.name):
            continue                                    # другой язык в тайтле (Java/C#/…) — мимо
        if APPLY_SKIP_GHOSTS and v.is_ghost():
            continue                                    # висит >60 дн — отклик бессмыслен
        snippet = rec.requirement
        is_remote_any = v.is_remote() or has_remote(v.name + " " + snippet)
        tier = _apply_tier(v, is_remote_any)
        if tier is None:
            continue                                    # стек/формат/город не подходят ни под один уровень
        out.append(Candidate(
            id=v.id, name=v.name, url=rec.url or f"https://hh.ru/vacancy/{v.id}",
            employer=v.employer, exp=v.experience, age=v.age_days(), desc=snippet, tier=tier,
        ))
    # приоритет: сначала строгий уровень целиком (tier), внутри — младший опыт, при равном — свежее
    out.sort(key=lambda c: (c.tier,
                            _EXP_ORDER.get(c.exp, _RANK_UNKNOWN) if c.exp else _RANK_UNKNOWN,
                            c.age if c.age is not None else _AGE_UNKNOWN))
    return out[:limit]
