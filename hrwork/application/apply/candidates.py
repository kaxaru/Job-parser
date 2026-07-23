"""Отбор вакансий под автоотклик (чистая логика, без браузера — тестируется напрямую).

Профиль отклика = config.RESUME_* (тот же, что в resume.js ленты). Фильтр по стеку/опыту/
удалёнке/свежести + чёрные списки по тайтлу. Вынесено из autoclick (god-модуль): здесь нет
Playwright, поэтому логику можно менять и тестировать без риска для реальных откликов."""
import math
import re
from dataclasses import dataclass
from enum import IntEnum

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
APPLY_EXPS        = {e for x in RESUME_EXP_IDS if (e := Experience.from_code(x))}
APPLY_SKIP_GHOSTS = True                                 # не откликаться на гост-вакансии (>60 дн)
# ВРЕМЕННО: не пытаться откликаться на вакансии-опросники повторно. Бот их не заполняет
# (вопросы работодателя специфичны), но раньше они НЕ исключались из выборки — очередь
# упиралась в них каждый прогон (19.07: 21 опросник из 56 кандидатов, отклик не двигался).
# Исключаем по форм-очереди, а НЕ через marks: в ленте они остаются с бейджем «форма»,
# отказом не помечаются, и снять фильтр можно одним флагом.
APPLY_SKIP_FORMS = True
# Чёрный список по ТАЙТЛУ: не откликаемся на ML/MLOps/senior-роли (и их рус-эквиваленты
# ведущий/старший/тимлид/lead — иначе «Ведущий backend» проскакивает мимо «senior»).
# \bml\b не ловит HTML/XML (нет границы перед ml); ловит «ML», «ML-инженер», «ML/DS».
APPLY_BLACKLIST = re.compile(
    r"\bml\b|\bml[\s\-/]?ops\b|\bmlops\b|\bsenior\b|\bсеньор|\bсиньор"
    r"|\bведущ|\bстарш|\bтимлид|\bteam.?lead\b|\blead\b|\bлид\b|\bprincipal\b|\bstaff\b",
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
# Роль «Аналитик» смешивает АЙТИШНЫХ аналитиков (данных/системный/data) и ДОМЕННЫХ
# (финансовый, логист, медицинский, консультант, по закупкам). Точечные регексы по каждому
# домену — бесконечный whack-a-mole, поэтому инвертируем: в отклики берём только явно
# айтишные подтипы, всё прочее в этой роли пропускаем. Другие роли (Backend/DevOps/QA/…)
# правилом не затронуты — там ложных срабатываний почти нет.
APPLY_ANALYST_OK = re.compile(
    r"аналитик\w*\s+данных|данн\w*\s+аналитик|систем\w*\s+аналит"
    r"|\bdata\b|дата.?аналит|data.?аналит",
    re.I)
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


def pick_candidates(records: list, marks: dict[str, str], limit: int,
                    form_ids: set[str] | frozenset = frozenset()) -> list["Candidate"]:
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
        if APPLY_BLACKLIST.search(v.name):
            continue                                    # ML/MLOps/senior — не наш уровень
        if APPLY_ROLE_BLACKLIST.search(v.name):
            continue                                    # риск/портфельный/планирование ресурсов — не инженерная роль
        if v.role is Role.ANALYST and not APPLY_ANALYST_OK.search(v.name):
            continue                                    # доменный аналитик (финансы/логистика/медицина/…)
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
    out.sort(key=lambda c: (c.tier, _EXP_ORDER.get(c.exp, _RANK_UNKNOWN),
                            c.age if c.age is not None else _AGE_UNKNOWN))
    return out[:limit]
