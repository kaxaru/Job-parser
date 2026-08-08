"""Доменный слой: типизированная вакансия и разметка навыков. БЕЗ БД —
чистые функции, переиспользуются всеми бэкендами и юнит-тестируются отдельно.

Единственный ввод-вывод спрятан за портом: курсы валют приходят в `Vacancy.from_raw`
аргументом `fx`, а достаёт их с диска адаптер `rates.py`. Ленивый дефолт (`fx=None`)
оставлен только потому, что `Pipeline.prepare` пока зовёт `from_raw(rec)` одним
аргументом; как только конвейер начнёт прокидывать курсы явно, дефолт можно убрать.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .rates import load_rates, resolve_currency, to_rub

# ── справочники нормализации ──
EXPERIENCE = {
    "noExperience": "Без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "6+ лет",
}
# Подписи ДОСЛОВНО как у родителя (`hrwork/domain/schedule.py::Schedule.label`), и это не
# косметика: с 09.08.2026 `flexible` считается удалёнкой, и подпись «Гибкий график» прятала
# бы это от человека, читающего витрину. Мёртвые коды `shift` и `flyInFlyOut` убраны —
# домен родителя их не производит (в `Schedule` три члена), и держать подписи для того,
# чего не бывает, значит обещать срез, которого никогда не будет.
# Незнакомый НЕпустой код проходит как есть (`_label`) — контракт docs/etl.md.
SCHEDULE = {
    "fullDay": "Офис",
    "remote": "Удалённо",
    "flexible": "Гибрид",
}

#: Подпись бакета «опыт не указан». Живёт здесь, потому что то же слово стоит литералом
#: в витринах (`coalesce(nullif(experience,''), 'не указан')`): SQL импортировать не умеет,
#: поэтому равенство стережёт тест, а не надежда.
NO_EXPERIENCE_LABEL = "не указан"

# Что считается удалёнкой в витринах: коды remote + flexible (гибрид). ЕДИНСТВЕННЫЙ ответ
# на вопрос «это удалёнка?» — дословная копия `hrwork/domain/schedule.py::REMOTE_LIKE_CODES`
# (`Schedule.is_remote_like` = REMOTE + HYBRID). До 09.08.2026 у стенда был СВОЙ, третий
# ответ («код remote ИЛИ маркер в тексте»), из-за чего доля удалёнки в витринах и в отчётах
# родителя расходились на два разряда одной и той же выборки.
# Копия, а не импорт: `etl/` монтируется в контейнер Airflow БЕЗ пакета `hrwork`
# (docker-compose: `./etl:/opt/airflow/etl`, `../data:ro`) — импорт родителя уронил бы DAG
# на старте, да и сам родитель признаёт `import hrwork.config` побочным (mkdir, dotenv,
# переконфигурация loguru, чтение resume_profile.json — см. шапку `hrwork/domain/parsing.py`).
# От расхождения копии защищает страж-тест
# `tests/test_domain.py::test_remote_like_codes_match_parent_domain`.
REMOTE_LIKE_CODES = ("remote", "flexible")

# Словесные маркеры удалёнки — ОТДЕЛЬНОЕ понятие (поле `remote_mentioned`), а не признак
# формата: у родителя это `parsing.py::has_remote`, из которого собирается `feed.py`
# `remote_any` для отбора под отклик. В аналитический срез «доля удалёнки» текст не входит.
REMOTE_MARKERS = (
    "удалённ", "удаленн", "удалёнк", "удаленк", "remote", "дистанцион",
    "из дома", "home office", "хоум офис", "work from home", "wfh", "из любой точки",
)

# Сигнатура словаря стека родителя (`hrwork/domain/parsing.py::DETECT_SIG`): под ней в
# сырых записях лежит готовый `_techs`. Совпала -> берём кеш, не совпала -> считаем сами
# (см. `detect_skills`). Пин, а не вычисление: посчитать сигнатуру можно только по
# `hrwork.config.TECH_PATTERNS`, которого в контейнере нет. Протухание пина ловит
# страж-тест `tests/test_domain.py::test_parent_detect_sig_pin_is_current`.
PARENT_DETECT_SIG = "7c2e86237020"

# ── словарь стека ──
# Стек берём из кеша родителя, свой словарь нужен для двух разных вещей — поэтому он
# разделён надвое (раньше был один на 37 тегов, разошедшийся с родителем на две трети).

# (1) Аналитические теги стенда: их в словаре родителя НЕТ, поэтому считаются ВСЕГДА —
# и поверх кеша, и в фолбэке. Ключи обязаны не пересекаться с родительскими, иначе это
# будет второе определение одного тега (страж-тест
# `test_etl_specific_tags_do_not_shadow_parent_tags`).
ETL_SKILL_PATTERNS = {
    "SQL": r"\bsql\b",
    "ETL": r"\betl\b",
    "Airflow": r"airflow",
    "Airbyte": r"airbyte",
    "dbt": r"\bdbt\b",
    "Greenplum": r"greenplum",
    "Hadoop": r"hadoop",
    "Spark": r"\bspark\b",
    "Pandas": r"\bpandas\b",
    "Superset": r"superset",
    "Metabase": r"metabase",
    "Power BI": r"power\s?bi",
    "Tableau": r"tableau",
    "Bash": r"\bbash\b",
    "Celery": r"\bcelery\b",
    "Git": r"\bgit\b",
    "Linux": r"\blinux\b",
    "REST/API": r"\brest\b|\bapi\b",
}

# (2) Фолбэк общего стека — для записей без валидного кеша (старый срез, фикстуры, чужой
# источник). Паттерны скопированы ДОСЛОВНО из `hrwork/config.py::TECH_PATTERNS`, страж-тест
# `test_stack_patterns_are_verbatim_copies_of_parent` сверяет их посимвольно. Это
# ПОДМНОЖЕСТВО (19 тегов из 55): полный словарь не копируем — на реальном срезе кеш валиден
# у всех записей, а лишняя копия данных живёт только чтобы разойтись с оригиналом.
STACK_SKILL_PATTERNS = {
    "Python": r"\bpython\b",
    "JavaScript": r"\bjavascript\b|\bjs\b",
    "TypeScript": r"\btypescript\b",
    "Java": r"\bjava\b(?!script)",
    # «go» — 2 буквы, ловит бренд «Яндекс Go» (курьеры/такси) и «go to». Родитель требует
    # dev-контекст рядом; тег называется Go (а не Golang), иначе витрина «спрос на стек»
    # и чипы ленты несравнимы по одному и тому же навыку.
    "Go": (r"\bgolang\b"
           r"|\bgo\b(?=\W{0,3}(?:developer|разраб|программист|engineer|backend|/\s*(?:php|python|java|node)))"
           r"|(?:разраб\w*|программист|developer|engineer|backend|бэкенд|fullstack|стек\w*|язык\w*|знание|опыт|владение|\bна|\bin|using)\W{1,3}go\b"),
    "C#": r"\bc#|\.net\b",
    "React": r"\breact\b",
    "Django": r"\bdjango\b",
    "FastAPI": r"\bfastapi\b",
    "Flask": r"\bflask\b",
    "PostgreSQL": r"\bpostgresql\b|\bpostgres\b",
    "MySQL": r"\bmysql\b",
    "MongoDB": r"\bmongodb\b",
    "Redis": r"\bredis\b",
    "ClickHouse": r"\bclickhouse\b",
    "Docker": r"\bdocker\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "Kafka": r"\bkafka\b",
    "RabbitMQ": r"\brabbitmq\b",
}

SKILL_PATTERNS = {**STACK_SKILL_PATTERNS, **ETL_SKILL_PATTERNS}
SKILL_RE = {n: re.compile(p, re.IGNORECASE | re.UNICODE) for n, p in SKILL_PATTERNS.items()}
ETL_SKILL_RE = {n: SKILL_RE[n] for n in ETL_SKILL_PATTERNS}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _label(table: dict[str, str], code) -> str | None:
    """Код внешнего справочника -> подпись. Пусто -> None, а НЕ пустая подпись.

    Родитель кодирует «не указано» пустой строкой (`Experience.from_code`: «Пусто/неизвестный
    код -> None»). Раньше `EXPERIENCE.get(code, code)` возвращал её как есть, и защита витрин
    `coalesce(experience, 'не указан')` не срабатывала никогда — в срез уходил бакет с пустой
    подписью. Неизвестный НЕпустой код по-прежнему проходит как есть (контракт docs/etl.md)."""
    return table.get(code, code) if code else None


def detect_skills(text: str, cached: list[str] | None = None) -> tuple[str, ...]:
    """Теги стека вакансии.

    `cached` — готовый стек родителя (`_techs`), уже сверенный по сигнатуре словаря. Есть
    кеш -> берём его и ДОБАВЛЯЕМ аналитические теги стенда (их у родителя нет); нет кеша ->
    считаем сами по своему словарю. Кеш в приоритете не ради скорости: пока стенд считал
    стек своим словарём, витрина «спрос на технологии» отвечала не на тот вопрос, что лента,
    и одну вакансию нельзя было сверить между ними."""
    found: dict[str, None] = {} if cached is None else dict.fromkeys(cached)
    patterns = SKILL_RE if cached is None else ETL_SKILL_RE
    for name, rx in patterns.items():
        if name not in found and rx.search(text):
            found[name] = None
    return tuple(found)


# Города-агрегаторы (hirify/talanto) приносят списки регионов до ~2400 симв. вместо города
# («Remote, Argentina, Bolivia, …»). Режем в ЕДИНОМ transform (не в адаптере) -> все движки
# получают одно значение и числа сходятся; 200 влезает в MSSQL NVARCHAR(200) + UNIQUE-индекс
# core.cities (лимит ключа ~1700 байт). Реальные города < 50 симв. — под кап не попадают.
CITY_MAX = 200


def _cap(s, n=CITY_MAX):
    return s[:n] if isinstance(s, str) and len(s) > n else s


@dataclass(frozen=True)
class Vacancy:
    """Нейтральное представление вакансии — общий контракт для всех хранилищ."""
    id: str
    source: str          # портал-источник из `_source`; реестр порталов ведёт родитель
    name: str
    city: str | None
    employer: str | None
    salary_min: float | None
    salary_max: float | None
    salary_min_rub: float | None   # то же в рублях по FX — единственное, что можно усреднять
    salary_max_rub: float | None
    salary_currency: str | None    # канонический код: RUR/BYR/USDT уже сведены
    salary_gross: bool | None
    experience: str | None
    schedule: str | None
    is_remote: bool                # удалёнкоподобность = remote + flexible (как у родителя)
    remote_mentioned: bool         # словесный маркер удалёнки в тексте — отдельное понятие
    url: str | None
    query: str | None
    skills: tuple[str, ...]

    @classmethod
    def from_raw(cls, rec: dict, fx: dict[str, float] | None = None) -> Vacancy:
        """Разбор сырой записи парсера в типизированную вакансию (бывш. _to_row).

        id — строка: основной проект неймспейсит id по источникам (`hirify_733072`,
        `talanto_<uuid>`), поэтому `int()` больше неприменим (ронял 2/3 записей).
        source — из служебного поля `_source` (дефолт 'hh' для legacy-записей без него);
        перечень порталов задаёт родитель (`hrwork/config.py::SOURCES`), домен его не
        хардкодит и не ограничивает — новый портал доезжает до витрин сам.
        fx — курсы валют per-USD для `salary_*_rub`; None -> адаптер `rates.load_rates()`.

        ПУСТАЯ СТРОКА внешнего источника = ОТСУТСТВИЕ значения (None), и приводится она
        здесь, один раз, а не в SQL-гейтах каждого движка: гейты вида
        `WHERE employer_name IS NOT NULL` пустую строку пропускали, и в измерении заводился
        безымянный работодатель, к которому джойнилась каждая седьмая вакансия."""
        sal = rec.get("salary") or {}
        area = rec.get("area") or {}
        emp = rec.get("employer") or {}
        exp = (rec.get("experience") or {}).get("id")
        sch = (rec.get("schedule") or {}).get("id")
        snippet = rec.get("snippet") or {}
        req = snippet.get("requirement") or "" if isinstance(snippet, dict) else ""
        text = " ".join([rec.get("name") or "", req, strip_html(rec.get("description_html") or "")])
        low = text.lower()
        techs = rec.get("_techs") if rec.get("_dv") == PARENT_DETECT_SIG else None
        cached = [t for t in techs if isinstance(t, str)] if isinstance(techs, list) else None
        currency = resolve_currency(sal.get("currency")) or None
        rates = fx if fx is not None else load_rates()
        smin, smax = _num(sal.get("from")), _num(sal.get("to"))
        return cls(
            id=str(rec["id"]),
            source=rec.get("_source") or "hh",
            name=rec.get("name") or "",
            city=_cap(area.get("name") or rec.get("_city") or None),
            employer=emp.get("name") or None,
            salary_min=smin,
            salary_max=smax,
            salary_min_rub=to_rub(smin, currency, rates),
            salary_max_rub=to_rub(smax, currency, rates),
            salary_currency=currency,
            salary_gross=sal.get("gross"),
            experience=_label(EXPERIENCE, exp),
            schedule=_label(SCHEDULE, sch),
            is_remote=sch in REMOTE_LIKE_CODES,
            remote_mentioned=any(m in low for m in REMOTE_MARKERS),
            url=rec.get("alternate_url") or None,
            query=rec.get("_query") or None,
            skills=detect_skills(text, cached),
        )
