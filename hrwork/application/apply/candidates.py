"""Отбор вакансий под автоотклик (чистая логика, без браузера — тестируется напрямую).

Профиль отклика = config.RESUME_* (тот же, что в resume.js ленты). Фильтр по стеку/опыту/
удалёнке/свежести + чёрные списки по тайтлу. Вынесено из autoclick (god-модуль): здесь нет
Playwright, поэтому логику можно менять и тестировать без риска для реальных откликов."""
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any

from hrwork.config import (
    APPLY_BLACKLISTS,
    APPLY_CORE_WIDE,
    APPLY_EXTRA_EXP_IDS,
    APPLY_OFFICE_CITIES,
    RESUME_CORE,
    RESUME_EXP_IDS,
    log,
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
# работы при темпе ~62 отклика/сутки; с «3–6 лет» пул 1532 (STRICT 461 · WIDE 292 · OFFICE
# 779 — сумма разбивки; до 08.08.2026 здесь стояло 1534, а в apply.md 1532). Порядок не меняется:
# `pick_candidates` сортирует по tier, а внутри — по грейду (младшие раньше, см. _EXP_ORDER),
# поэтому старшая вилка разбирается последней и только когда junior-кандидаты кончились.
APPLY_EXTRA_EXPS  = set(APPLY_EXTRA_EXP_IDS)   # из профиля (`extra_exp_ids`), дефолт «3–6 лет»


def _exps_from_codes(codes: Iterable[str]) -> set[Experience]:
    """Коды опыта -> VO, с ЖАЛОБОЙ на непризнанный код.

    Мягкий парсер (`Experience.from_code`) применён к НАШЕЙ константе из профиля, а не к
    данным портала, поэтому молча пропускать нераспознанное нельзя: пропуск сужает отбор под
    отклик (= недоотклики), и наблюдаемости у такого сужения нет никакой — пул просто меньше.
    `config.py::_known_exp_ids` фильтрует те же коды на входе; здесь второй рубеж, на случай
    когда набор пришёл не оттуда."""
    out: set[Experience] = set()
    for x in codes:
        e = Experience.from_code(x)
        if e is None:
            log.warning("APPLY_EXPS: код опыта {} не распознан — исключён из отбора", x)
            continue
        out.add(e)
    return out


APPLY_EXPS        = _exps_from_codes((*RESUME_EXP_IDS, *APPLY_EXTRA_EXPS))
APPLY_SKIP_GHOSTS = True                                 # не откликаться на гост-вакансии (>60 дн)
# ВРЕМЕННО: не пытаться откликаться на вакансии-опросники повторно. Бот их не заполняет
# (вопросы работодателя специфичны), но раньше они НЕ исключались из выборки — очередь
# упиралась в них каждый прогон (19.07: 21 опросник из 56 кандидатов, отклик не двигался).
# Исключаем по форм-очереди, а НЕ через marks: в ленте они остаются с бейджем «форма»,
# отказом не помечаются, и снять фильтр можно одним флагом.
APPLY_SKIP_FORMS = True
# ── Чёрные списки: дефолт здесь, переопределение — в resume_profile.json ──────────────
# Регексы ниже это ПРЕДПОЧТЕНИЯ КОНКРЕТНОГО СОИСКАТЕЛЯ, а не логика приложения. Владелец
# профиля меняет их, не трогая код: ключ в `blacklists` профиля побеждает дефолт, пустой
# список выключает правило целиком. Так чужой форк не конфликтует с апстримом при обновлении.
def _words_to_rx(words: list[str]) -> str:
    """СПИСОК СЛОВ -> регекс. Основной способ задать правило: писать регулярки ради своих
    предпочтений человек не должен, а именно это профиль и требовал до 08.08.2026.

    Слово матчится целиком (`QA` не ловит `qa` внутри `Aqua`), хвост `*` даёт совпадение
    по началу — под русскую морфологию: `тестировщ*` это и «тестировщик», и «тестировщика».
    Границы ставятся только там, где они работают: `\\b` требует буквенно-цифрового соседа,
    поэтому у `C#` и `.NET` соответствующий край остаётся открытым (иначе правило молча
    не срабатывало бы никогда)."""
    parts = []
    for raw in words:
        w = str(raw).strip()
        if not w:
            continue
        prefix = w.endswith("*")
        w = w[:-1] if prefix else w
        if not w:
            continue
        pat = re.escape(w)
        if w[0].isalnum() or w[0] == "_":
            pat = r"\b" + pat
        if not prefix and (w[-1].isalnum() or w[-1] == "_"):
            pat += r"\b"
        parts.append(pat)
    return "|".join(parts)


def _rx(key: str, default: str) -> "re.Pattern[str]":
    """Регекс правила: из профиля, иначе дефолт.

    В профиле ждём СПИСОК СЛОВ (см. `_words_to_rx`). Строка тоже принимается и трактуется
    как готовый регекс — так записаны дефолты ниже и так удобно тем, кому нужен lookaround.
    Битый регекс НЕ роняет сбор: падаем на дефолт с предупреждением, потому что отладить
    его владельцу профиля нечем, а тихо отключить правило отбора хуже, чем взять наше."""
    raw = APPLY_BLACKLISTS.get(key)
    if raw is None:
        return re.compile(default, re.I)
    # Пусто = «правило мне не нужно». `(?!)` не матчится никогда — это честнее, чем пустой
    # паттерн, который совпадает с ЛЮБОЙ строкой и отсеял бы всё подряд.
    if isinstance(raw, list):
        return re.compile(_words_to_rx(raw) or r"(?!)", re.I)
    if not raw.strip():
        return re.compile(r"(?!)")
    try:
        return re.compile(raw, re.I)
    except re.error as e:
        log.warning("resume_profile.blacklists.{}: неверный регекс ({}) — беру дефолт", key, e)
        return re.compile(default, re.I)


# Чёрный список по ГРЕЙДУ: senior-роли и рус-эквиваленты (ведущий/старший/тимлид/lead —
# иначе «Ведущий backend» проскакивает мимо «senior»).
#
# `\bsr\b` и «принципал» добавлены 08.08.2026 (аудит): их знал словарь грейдов
# `domain/grade.py::_TITLE_RX`, но не этот список, и «Sr. Python Developer» проходил отбор
# как рядовая вакансия, а для зарплатного ответа считался senior. Один тайтл — две трактовки
# в одном прогоне. Набор слов синхронизирован с `resume_profile.example.json::blacklists.senior`
# (страж паритета — tests/backend/test_profile_settings.py).
APPLY_SENIOR_BLACKLIST = _rx("senior",
    r"\bsenior\b|\bсеньор|\bсиньор|\bsr\b"
    r"|\bведущ|\bстарш|\bтимлид|\bteam.?lead\b|\blead\b|\bлид\b"
    r"|\bprincipal\b|принципал|\bstaff\b")

# ── СТАЖИРОВКИ: ниже целевого грейда ───────────────────────────────────────────────────
# Задано пользователем 08.08.2026. Отсекается стажировка, НЕ junior: junior — это штатная
# позиция, ради которой отбор и настроен, а стажировка обычно означает срочный договор,
# учебную нагрузку и оплату ниже рынка. Слова подобраны так, чтобы `junior` не задеть.
#
# Почему списком слов, а не префиксом `intern*`: `\bintern\b` ловит «Intern» и не ловит
# «internal» и «international» (после `intern` идёт словесный символ), а `intern*` поймал бы
# оба — «Internal Tools Developer» вылетел бы из отбора ни за что. `internship` вынесен
# отдельным словом по той же причине.
#
# Известный побочный эффект: комбинированный тайтл «Junior/Intern Python Developer» будет
# отсеян — правило смотрит на вхождение в тайтл, а не на «какой грейд главнее». Это
# сознательно: такие вакансии почти всегда идут по стажёрской ставке. Нужно иначе — ключ
# `internship` в профиле правится словами, исходник трогать не надо.
APPLY_INTERNSHIP_BLACKLIST = _rx("internship",
    r"\bintern\b|\binterns\b|\binternship|\btrainee\b"
    r"|стаж[её]р|стажиров|практикант")

# ── ИНЖЕНЕРИЯ НЕ ПРО СОФТ ──────────────────────────────────────────────────────────────
# Задано пользователем 09.08.2026 по конкретному случаю: крон откликнулся на «Инженер-химик»
# (hh.ru/vacancy/135163990, «А Плюс»). Разбор показал НЕ дыру в блеклисте, а обход гейта
# специализации: `parsing.py::_detect_role` считает роль по тайтлу И ТЕХАМ ИЗ ОПИСАНИЯ, и
# упоминание Python в описании химической вакансии превращает `Role.NON_IT` в
# `Role.DEVELOPER` — после чего проверка `if not v.role.is_it` в `pick_candidates`
# пропускает её. Это рецидив инцидента из `docs/errors.md` («Продюсер AI-видео» и
# «Инженер-испытатель»: язык из ОПИСАНИЯ легитимизировал не-IT роль).
#
# Правило ставится на ТАЙТЛ, а не на роль, именно поэтому: `out_of_scope` техов не видит,
# и обойти его описанием нельзя. Замер по 128 752 карточкам сборки 09.08.2026: `химик`
# ловит 98 тайтлов, среди них ни одного программистского — совпадения вида
# «Химик-технолог/химик-разработчик в области бытовой химии» это химия, а не разработка.
APPLY_OTHER_ENGINEERING_BLACKLIST = _rx("other_engineering", r"\bхимик")

# ── РУКОВОДЯЩИЕ должности: всё, что выше Lead ──────────────────────────────────────────
# Задано пользователем 07.08.2026: интересны только стандартные инженерные позиции до
# senior включительно. Грейдовый список выше ловит senior/lead, здесь — управленческая
# ветка: руководитель группы/отдела/направления, Head of, Director, VP, C-level, Manager.
# Замер на живом пуле (646 кандидатов): 36 таких тайтлов проходили.
#
# `architect` тоже здесь: в лестницах грейдов он идёт вровень со staff/principal, которые
# уже отсекаются выше, — держать его отдельно было бы непоследовательно.
APPLY_MANAGEMENT_BLACKLIST = _rx("management",
    r"руководител|начальник|директор|заведующ|заместител"
    r"|\bhead\s+of\b|\bhead\b|\bdirector\b|\bvp\b|vice\s+president|\bchief\b"
    r"|\bcto\b|\bceo\b|\bcoo\b|\bcio\b|\bcpo\b|\bexecutive\b|\bfounder\b"
    r"|\bmanager\b|\bmanagement\b|\bsupervisor\b|\barchitect\b|архитектор")
# Инженерные существительные — если такое стоит в тайтле РАНЬШЕ управленческого слова,
# роль инженерная, а управленческое слово относится к чему-то ещё. Реальный случай из пула:
# «Python-разработчик (AI-агент Операционный директор)» — вакансия разработчика, «директор»
# в НАЗВАНИИ ПРОДУКТА. Та же логика, что у APPLY_ROLE_BLACKLIST: целимся в роль, не в домен.
_ENGINEER_NOUN = re.compile(
    r"разработчик|программист|инженер|\bdeveloper\b|\bengineer\b|\bdev\b|\bsre\b|\bdevops\b",
    re.I)


def _is_management(name: str) -> bool:
    """Управленческая ли роль. False, если инженерное существительное стоит РАНЬШЕ
    управленческого слова, — тогда управленческое относится не к роли (см. _ENGINEER_NOUN)."""
    m = APPLY_MANAGEMENT_BLACKLIST.search(name)
    if not m:
        return False
    eng = _ENGINEER_NOUN.search(name)
    return not (eng and eng.start() < m.start())

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
APPLY_TARGET_ENGINEERING = _rx("target_engineering",
    r"\bllm\b|\brag\b|data\s+engineer|дата.?инженер|инженер\w*\s+данных|\bdwh\b|\betl\b")
# QA целиком и БЕЗУСЛОВНО, включая QA с Python в тайтле (в пуле их 10 из 114).
APPLY_QA_BLACKLIST = _rx("qa",
    r"\bqa\b|\baqa\b|\bqc\b|\bsdet\b|quality\s+assurance|test\w*\s+engineer"
    r"|тестировщ|тестирован|автотест")
# Аналитики целиком. Прежняя логика была ОБРАТНОЙ (APPLY_ANALYST_OK пропускал айтишные
# подтипы) — отменена сознательно: аналитика не входит в целевую специализацию.
APPLY_ANALYST_BLACKLIST = _rx("analyst", r"аналитик|analyst|\bbi\b")
# ML/DS. LLM и RAG сюда НЕ входят — они целевые, см. APPLY_TARGET_ENGINEERING выше:
# «Data Scientist (NLP / LLM)» остаётся в пуле по маркеру LLM.
# \bml\b не ловит HTML/XML (нет границы перед ml); ловит «ML», «ML-инженер», «ML/DS».
APPLY_ML_BLACKLIST = _rx("ml",
    r"\bml\b|\bml[\s\-/]?ops\b|\bmlops\b|\bmle\b|machine\s+learning|машинн\w*\s+обучени"
    r"|data\s+scientist|дата.?са[йи]ентист|deep\s+learning|computer\s+vision|\bnlp\b")
# DevOps / SRE / инфраструктура — эксплуатация, а не разработка (задано 07.08.2026:
# «больше интересует писать код»). Замер на живом пуле: 127 таких тайтлов из 583, причём
# 123 — чистая инфраструктура и лишь 4 гибрида, где рядом стоит код.
APPLY_DEVOPS_BLACKLIST = _rx("devops",
    r"\bdevops\b|\bdevsecops\b|\bsre\b|site\s+reliability|\bplatform\s+engineer\b"
    r"|систем\w*\s+администратор|сисадмин|\bsysadmin\b|инфраструктур")
# Маркер «здесь пишут код» — снимает devops-запрет. Отдельно от APPLY_TARGET_ENGINEERING:
# тот про целевой домен (LLM/DWH/ETL), а этот про сам характер работы. Реальные гибриды
# из пула: «Инженер-программист по безопасной разработке / Middle DevSecOps»,
# «Разработчик в инфраструктуру симулятора», «LLM Platform Engineer».
_CODE_MARKER = re.compile(
    r"\bpython\b|\bbackend\b|бэкенд|бекенд|\bdeveloper\b|разработчик|программист"
    r"|\bfullstack\b|\bfull.stack\b|\bllm\b|data\s+engineer",
    re.I)
# «Ищу только python»: даже если Python есть в требованиях (как «плюс»), вакансию с
# ДРУГИМ основным языком в ТАЙТЛЕ не берём — иначе «Java-разработчик (+Python)» проскочит.
# Только backend-конкуренты Python; JS/TS/React не считаем (Python-fullstack ок).
APPLY_LANG_BLACKLIST = _rx("other_lang",
    r"\bjava\b(?!script)|\bc#|\bc\+\+|\bcpp\b|\bphp\b|\bgolang\b|\bscala\b|\bruby\b"
    r"|\bkotlin\b|\brust\b|\bdelphi\b|\bperl\b|\.net\b|\b1с\b|\b1c\b|\bgo[-\s]?разраб")
# НЕ-ИНЖЕНЕРНЫЕ роли, формально проходящие как IT: `_apply_tier` смотрит только на СТЕК и
# формат, поэтому «Риск-аналитик» с упоминанием Python в JD получал tier STRICT. Роль
# (`Аналитик`) тут не помогает — она таксономия РЫНКА для дашборда, а это предпочтения
# ОТКЛИКА, поэтому фильтр живёт в apply-слое.
# Целимся в РОЛЬ-существительное (аналитик/менеджер), а не в домен: «Разработчик моделей
# оценки кредитных рисков» — настоящая dev-вакансия и проходить обязана.
APPLY_ROLE_BLACKLIST = _rx("non_engineering",
    r"риск.?(?:аналит|менеджер)"                  # Риск-аналитик / Риск-менеджер по аналитике
    r"|портфельн\w*\s*(?:аналит|менеджер)"        # Портфельный аналитик/портфельный менеджер
    r"|планировани\w*.{0,25}ресурс")              # Аналитик по планированию и управлению ресурсами
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


class OutOfScope(Enum):
    """Почему вакансия не проходит отбор ПО ТАЙТЛУ — VO, а не строка-литерал.

    Причина уходит в логи и в сводку чистки очереди, то есть это доменное понятие с
    конечным набором значений; голая строка здесь была бы тем же дефектом, что `tier: int`
    с магическим `0` до появления `ApplyTier`. Значение = подпись для человека."""
    SENIOR = "senior/lead"
    INTERNSHIP = "стажировка"
    OTHER_ENGINEERING = "инженерия не про софт"
    MANAGEMENT = "руководящая"
    NON_ENGINEERING = "не инженерная роль"
    QA = "QA"
    ANALYST = "аналитик"
    ML = "ML/DS"
    DEVOPS = "DevOps/SRE"
    OTHER_LANG = "другой язык"

    @property
    def label(self) -> str:
        return self.value


def _no_target_marker(name: str) -> bool:
    """Нет инженерного маркера (LLM/RAG/Data Engineer/DWH/ETL) — исключение не применяется."""
    return not APPLY_TARGET_ENGINEERING.search(name)


# Правила отбора по тайтлу — ТАБЛИЦА, а не лестница `if`. Порядок значим и виден целиком:
# QA проверяется до аналитики и ML, потому что на него исключение для инженерных гибридов
# не распространяется («QA Engineer (LLM-платформа)» — это тестирование). Новое правило =
# строка здесь плюс член OutOfScope, ветвление трогать не нужно.
_SCOPE_RULES: tuple[tuple[OutOfScope, Callable[[str], bool]], ...] = (
    (OutOfScope.SENIOR,          lambda n: bool(APPLY_SENIOR_BLACKLIST.search(n))),
    # Рядом с грейдовым правилом и до остальных: «Стажёр QA» интереснее назвать стажировкой,
    # чем тестированием — причина уходит в лог и в сводку чистки очереди.
    (OutOfScope.INTERNSHIP,      lambda n: bool(APPLY_INTERNSHIP_BLACKLIST.search(n))),
    # Раньше грейдовых исключений: «Химик-разработчик» — химия, а не разработка, и причина
    # в сводке должна называться так.
    (OutOfScope.OTHER_ENGINEERING,
                                 lambda n: bool(APPLY_OTHER_ENGINEERING_BLACKLIST.search(n))),
    (OutOfScope.MANAGEMENT,      _is_management),
    (OutOfScope.NON_ENGINEERING, lambda n: bool(APPLY_ROLE_BLACKLIST.search(n))),
    (OutOfScope.QA,              lambda n: bool(APPLY_QA_BLACKLIST.search(n))),
    (OutOfScope.ANALYST,         lambda n: _no_target_marker(n)
                                 and bool(APPLY_ANALYST_BLACKLIST.search(n))),
    (OutOfScope.ML,              lambda n: _no_target_marker(n)
                                 and bool(APPLY_ML_BLACKLIST.search(n))),
    # Код в тайтле снимает devops-запрет: «LLM Platform Engineer» и «Инженер-программист
    # по безопасной разработке / Middle DevSecOps» — это разработка, а не эксплуатация.
    (OutOfScope.DEVOPS,          lambda n: not _CODE_MARKER.search(n)
                                 and bool(APPLY_DEVOPS_BLACKLIST.search(n))),
    (OutOfScope.OTHER_LANG,      lambda n: "python" not in n.lower()
                                 and bool(APPLY_LANG_BLACKLIST.search(n))),
)


def out_of_scope(name: str) -> OutOfScope | None:
    """Причина, по которой вакансия с таким ТАЙТЛОМ вне целевой специализации (или None).

    Единый источник правды для всех путей отклика: и для отбора кандидатов
    (`pick_candidates`), и для чистки форм-очереди (`forms.clean_queue`). Раньше очередь
    анкет жила по своим правилам и копила то, на что отклик всё равно не пошёл бы:
    на 07.08.2026 из 78 накопленных 32 были QA, аналитиками и руководителями.

    Только по тайтлу — намеренно: в форм-очереди кроме имени и ссылки ничего нет, а
    поднимать ради чистки весь кеш вакансий (395 МБ) незачем. Проверки, которым нужна
    доменная `Vacancy` (роль, грейд, свежесть, стек), остаются в `pick_candidates`."""
    for reason, hit in _SCOPE_RULES:
        if hit(name):
            return reason
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
        if out_of_scope(v.name):
            continue                                    # грейд/руководящая/QA/аналитик/ML/язык
        # Роль-детектор ловит то, чего нет в тайтле; на исключение для инженерных
        # гибридов («Data Engineer/Data Analyst») это правило не распространяется.
        if v.role is Role.QA:
            continue
        if v.role is Role.ANALYST and not APPLY_TARGET_ENGINEERING.search(v.name):
            continue
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
