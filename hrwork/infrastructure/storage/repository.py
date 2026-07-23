"""Репозиторий вакансий (F3): прячет «JSON vs Postgres» и владеет сырым payload'ом.

Доменная `Vacancy` — чистая (зарплата/опыт/формат/свежесть). Всё, что нужно ДЛЯ ПОКАЗА
(описание, url) и для инкрементального enrich (маркер изменения, метка дозагрузки), живёт
в `VacancyRecord` — это забота слоя хранения, а не сущности. Потребители просят
`repo.load() -> list[VacancyRecord]`; парсинг сырого dict'а происходит РАЗ, внутри репозитория
(раньше каждый читатель звал parse_vacancy сам).

`JsonVacancyRepository` сериализует record <-> каноническая raw-схема (та же, что писали
источники), поэтому существующий vacancies_raw.json читается без пере-сбора. Замена на
`PgVacancyRepository` не трогает потребителей — они зависят от протокола, не от файла.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from hrwork.config import RAW_FILE
from hrwork.domain.models import Vacancy
from hrwork.domain.parsing import parse_vacancy

from .files import save_meta
from .jsonio import atomic_write_json, read_json_or


@dataclass
class VacancyRecord:
    """Собранная запись: доменная `Vacancy` + сырой payload портала + метаданные enrich.

    Domain остаётся чистым; описание/url/маркеры кеша — здесь, в слое хранения."""
    vacancy: Vacancy
    url: str = ""                    # ссылка на карточку (alternate_url)
    description_html: str = ""       # полное описание (HTML) — для модалки ленты
    requirement: str = ""            # текст сниппета/карточки (детект стека + фолбэк описания)
    sig: str = ""                    # маркер изменения вакансии (инкрементальный enrich)
    enriched: bool = False           # получено ли полное описание (не tldr-заглушка)
    enriched_at: str | None = None   # когда реально дозагружено (для max-age кеша)

    @property
    def id(self) -> str:
        return self.vacancy.id


@runtime_checkable
class VacancyRepository(Protocol):
    """Контракт хранилища вакансий — единственное, от чего зависят потребители."""

    def exists(self) -> bool: ...
    def load(self) -> list[VacancyRecord]: ...
    def save(self, records: list[VacancyRecord]) -> None: ...


class JsonVacancyRepository:
    """JSON-файл (vacancies_raw.json). Сериализует record <-> каноническая raw-схема."""

    def __init__(self, path: Path = RAW_FILE):
        self._path = path

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> list[VacancyRecord]:
        data = read_json_or(self._path, [])
        return [self._from_dict(d) for d in data]

    def save(self, records: list[VacancyRecord]) -> None:
        # Атомарно (tmp + os.replace): крэш посреди записи не портит файл многочасового сбора.
        atomic_write_json(self._path, [self._to_dict(r) for r in records])
        save_meta(len(records))

    # ── ACL: сырой dict <-> record (единственная точка, где живёт raw-схема) ──
    @staticmethod
    def _from_dict(d: dict) -> VacancyRecord:
        return VacancyRecord(
            vacancy=parse_vacancy(d),
            url=d.get("alternate_url", "") or "",
            description_html=d.get("description_html", "") or "",
            requirement=(d.get("snippet") or {}).get("requirement", "") or "",
            sig=d.get("_sig", "") or "",
            enriched=bool(d.get("_enriched")),
            enriched_at=d.get("_enriched_at"),
        )

    @staticmethod
    def _to_dict(r: VacancyRecord) -> dict:
        v = r.vacancy
        s = v.salary
        return {
            "id": v.id,
            "name": v.name,
            "area": {"id": v.city_id, "name": v.city},
            # зарплата уже net (Vacancy хранит net) -> gross=False; round-trip даёт те же net
            "salary": ({"from": s.frm, "to": s.to, "currency": s.currency, "gross": False}
                       if s else None),
            "experience": {"id": v.experience.hh_id if v.experience else ""},
            "schedule": {"id": v.schedule.hh_code},
            "snippet": {"requirement": r.requirement, "responsibility": ""},
            "description_html": r.description_html,
            "alternate_url": r.url,
            "employer": {"name": v.employer},
            "created_at": v.created_at,
            "published_at": v.published_at,
            "responses": v.responses,
            "_source": v.source,
            "_sig": r.sig,
            "_enriched": r.enriched,
            "_enriched_at": r.enriched_at,
        }


def record_from_vacancy(vacancy: Vacancy, **payload) -> VacancyRecord:
    """Хелпер для источников (F2b): собрать record из уже смапленной Vacancy + payload."""
    return VacancyRecord(vacancy=vacancy, **payload)


def vacancy_repository() -> VacancyRepository:
    """Единая точка создания репозитория (DIP): потребители зовут её, а не конкретный класс —
    подмена бэкенда (Postgres) остаётся правкой ровно этой функции (аудит 2026-07-22:
    JsonVacancyRepository инстанцировался в 6 местах по трём слоям)."""
    return JsonVacancyRepository()
