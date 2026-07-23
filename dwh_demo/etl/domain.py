"""Доменный слой: типизированная вакансия и разметка навыков. БЕЗ БД и I/O —
чистые функции, переиспользуются всеми бэкендами и юнит-тестируются отдельно.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

# ── справочники нормализации ──
EXPERIENCE = {
    "noExperience": "Без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "6+ лет",
}
SCHEDULE = {
    "fullDay": "Полный день",
    "remote": "Удалённо",
    "flexible": "Гибкий график",
    "shift": "Сменный график",
    "flyInFlyOut": "Вахта",
}
REMOTE_MARKERS = (
    "удалённ", "удаленн", "удалёнк", "удаленк", "remote", "дистанцион",
    "из дома", "home office", "хоум офис", "work from home", "wfh", "из любой точки",
)
SKILL_PATTERNS = {
    "Python": r"\bpython\b", "SQL": r"\bsql\b",
    "PostgreSQL": r"postgre\s?sql|\bpostgres\b", "MySQL": r"\bmysql\b",
    "ClickHouse": r"clickhouse|кликхаус", "Airflow": r"airflow", "ETL": r"\betl\b",
    "Docker": r"\bdocker\b", "Kubernetes": r"kubernetes|\bk8s\b", "FastAPI": r"fastapi",
    "Django": r"django", "Flask": r"\bflask\b", "Pandas": r"\bpandas\b",
    "Spark": r"\bspark\b", "Kafka": r"\bkafka\b", "Airbyte": r"airbyte", "dbt": r"\bdbt\b",
    "Greenplum": r"greenplum", "Hadoop": r"hadoop", "Redis": r"\bredis\b", "MongoDB": r"mongo",
    "Git": r"\bgit\b", "Linux": r"\blinux\b", "REST/API": r"\brest\b|\bapi\b",
    "C#": r"c#|\.net\b", "Java": r"\bjava\b", "JavaScript": r"javascript|\bjs\b",
    "TypeScript": r"typescript", "Golang": r"golang|\bgo\b(?=\W)", "React": r"\breact\b",
    "Superset": r"superset", "Metabase": r"metabase", "Power BI": r"power\s?bi",
    "Tableau": r"tableau", "Bash": r"\bbash\b", "Celery": r"\bcelery\b", "RabbitMQ": r"rabbitmq",
}
SKILL_RE = {n: re.compile(p, re.IGNORECASE | re.UNICODE) for n, p in SKILL_PATTERNS.items()}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _num(x):
    return x if isinstance(x, (int, float)) else None


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
    source: str          # портал-источник: hh | hirify | talanto (из raw-поля _source)
    name: str
    city: str | None
    employer: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_gross: bool | None
    experience: str | None
    schedule: str | None
    is_remote: bool
    url: str | None
    query: str | None
    skills: tuple[str, ...]

    @classmethod
    def from_raw(cls, rec: dict) -> Vacancy:
        """Разбор сырой записи HH в типизированную вакансию (бывш. _to_row).

        id — строка: основной проект неймспейсит id по источникам (`hirify_733072`,
        `talanto_<uuid>`), поэтому `int()` больше неприменим (ронял 2/3 записей).
        source — из служебного поля `_source` (дефолт 'hh' для legacy-записей без него)."""
        sal = rec.get("salary") or {}
        area = rec.get("area") or {}
        emp = rec.get("employer") or {}
        exp = (rec.get("experience") or {}).get("id")
        sch = (rec.get("schedule") or {}).get("id")
        snippet = rec.get("snippet") or {}
        req = snippet.get("requirement") or "" if isinstance(snippet, dict) else ""
        text = " ".join([rec.get("name") or "", req, strip_html(rec.get("description_html") or "")])
        low = text.lower()
        return cls(
            id=str(rec["id"]),
            source=rec.get("_source") or "hh",
            name=rec.get("name") or "",
            city=_cap(area.get("name") or rec.get("_city")),
            employer=emp.get("name"),
            salary_min=_num(sal.get("from")),
            salary_max=_num(sal.get("to")),
            salary_currency=sal.get("currency"),
            salary_gross=sal.get("gross"),
            experience=EXPERIENCE.get(exp, exp),
            schedule=SCHEDULE.get(sch, sch),
            is_remote=(sch == "remote") or any(m in low for m in REMOTE_MARKERS),
            url=rec.get("alternate_url"),
            query=rec.get("_query"),
            skills=tuple(name for name, rx in SKILL_RE.items() if rx.search(text)),
        )
