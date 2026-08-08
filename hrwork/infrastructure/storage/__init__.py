"""Персистентность: сырые данные + мета-кеш (files), атомарный JSON-IO (jsonio), отметки (marks).

Публичный API реэкспортируется здесь — потребители пишут `from hrwork.infrastructure.storage import load_raw`.
"""
from .files import (
    cache_hit_matches,
    cache_hit_usable,
    cache_valid,
    load_desc_cache,
    load_pre_dedup_counts,
    load_raw,
    now_iso,
    save_meta,
    save_raw,
)
from .jsonio import atomic_write_json, read_json_or
from .marks import MARK_VALUES, load_marks, save_marks
from .repository import (
    JsonVacancyRepository,
    VacancyRecord,
    VacancyRepository,
    record_from_vacancy,
    vacancy_repository,
)

__all__ = [
    "MARK_VALUES",
    "JsonVacancyRepository",
    "VacancyRecord",
    "VacancyRepository",
    "atomic_write_json",
    "cache_hit_matches",
    "cache_hit_usable",
    "cache_valid",
    "load_desc_cache",
    "load_marks",
    "load_pre_dedup_counts",
    "load_raw",
    "now_iso",
    "read_json_or",
    "record_from_vacancy",
    "save_marks",
    "save_meta",
    "save_raw",
    "vacancy_repository",
]
