"""Инкрементальный кеш описаний: схема записи и два разных вопроса к ней.

Кеш решает две задачи, которые легко перепутать:
  * «это описание про ТУ ЖЕ версию вакансии?» — `cache_hit_matches`;
  * «его ещё можно считать актуальным?» — `cache_hit_usable` (то же плюс возраст).
Разница не косметическая: протухшее описание всё равно лучше tldr-заглушки, когда бюджета
на пере-обогащение не осталось, а описание от ДРУГОЙ версии вакансии не годится никогда.

Второй предмет — необязательный ключ грейда (`experience_id`). Двухфазные источники
(getmatch) получают грейд только в карточке; без него переиспользование описания из кеша
молча теряло опыт у ~740 записей.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from hrwork.infrastructure import storage
from hrwork.infrastructure.storage import files
from hrwork.infrastructure.storage.files import (
    cache_hit_matches,
    cache_hit_usable,
    load_desc_cache,
)


def _raw(**kw):
    base = {
        "id": "getmatch_1911",
        "name": "Python-разработчик",
        "snippet": {"requirement": "выжимка", "responsibility": ""},
        "description_html": "<p>Полное описание</p>",
        "experience": {"id": "between3And6"},
        "_source": "getmatch",
        "_sig": "2026-08-01T10:00:00+00:00",
        "_enriched": True,
        "_enriched_at": "2026-08-01T10:05:00+00:00",
    }
    return {**base, **kw}


def _write(tmp_path, monkeypatch, items):
    path = tmp_path / "vacancies_raw.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    monkeypatch.setattr(files, "RAW_FILE", path)


# ── схема записи кеша ──────────────────────────────────────────────────────────

def test_desc_cache_record_carries_grade_from_the_card(tmp_path, monkeypatch):
    # Схема записи зафиксирована контрактом РОВНО этими ключами: sig, description_html,
    # requirement, at, experience_id
    _write(tmp_path, monkeypatch, [_raw()])
    assert load_desc_cache() == {
        "getmatch_1911": {
            "sig": "2026-08-01T10:00:00+00:00",
            "description_html": "<p>Полное описание</p>",
            "requirement": "выжимка",
            "at": "2026-08-01T10:05:00+00:00",
            "experience_id": "between3And6",
        }
    }


def test_desc_cache_promises_no_raw_grade_fields(tmp_path, monkeypatch):
    """АУДИТ 08.08.2026 (находка 32): запись отдавала ещё и `seniority`/`years`, но их никто
    не персистит (`repository.py::_to_dict` таких ключей не пишет), поэтому они ВСЕГДА были
    None. Контракт, обещающий то, чего нет, дороже отсутствующего: читатель заводит под него
    вторую ветку разбора. Грейд восстанавливается через `experience_id` — код доменного VO,
    который лежит в raw-схеме с самого начала."""
    _write(tmp_path, monkeypatch, [_raw(_seniority="senior", _years=5)])
    hit = load_desc_cache()["getmatch_1911"]
    assert set(hit) == {"sig", "description_html", "requirement", "at", "experience_id"}


def test_desc_cache_record_written_before_grade_fields_reads_as_no_grade(tmp_path, monkeypatch):
    # Старая запись без опыта обязана читаться без ошибки — просто без грейда, а не падением
    _write(tmp_path, monkeypatch, [_raw(experience={"id": ""})])
    assert load_desc_cache()["getmatch_1911"]["experience_id"] is None


def test_desc_cache_reads_the_grade_already_persisted_in_the_raw_schema(tmp_path, monkeypatch):
    # `experience.id` лежит в vacancies_raw.json с самого начала (repository.py::_to_dict),
    # поэтому грейд из кеша поднимается уже на текущем файле, без пере-сбора
    _write(tmp_path, monkeypatch, [_raw()])
    assert load_desc_cache()["getmatch_1911"]["experience_id"] == "between3And6"


def test_desc_cache_skips_tldr_stubs(tmp_path, monkeypatch):
    # tldr-заглушка (_enriched=False) не должна блокировать будущую дозагрузку
    _write(tmp_path, monkeypatch, [_raw(id="hirify_1", _enriched=False),
                                   _raw(id="hirify_2", description_html="")])
    assert load_desc_cache() == {}


# ── два вопроса к записи ───────────────────────────────────────────────────────

_NOW = datetime.now(tz=timezone.utc)
_FRESH = {"sig": "s1", "description_html": "<p>x</p>", "at": (_NOW - timedelta(days=1)).isoformat()}
_STALE = {"sig": "s1", "description_html": "<p>x</p>", "at": (_NOW - timedelta(days=20)).isoformat()}
_EMPTY = {"sig": "s1", "description_html": "", "at": (_NOW - timedelta(days=1)).isoformat()}


def test_both_questions_are_public_api_of_the_storage_layer():
    """Адаптер спрашивает предикаты через фасад `storage`, а не импортом подмодуля
    `storage.files`: обход фасада — единственное место, где слой протекал наружу
    (hirify тянул `cache_hit_matches` напрямую, потому что фасад правил другой владелец)."""
    assert (storage.cache_hit_matches, storage.cache_hit_usable) == \
           (cache_hit_matches, cache_hit_usable)
    assert {"cache_hit_matches", "cache_hit_usable"} <= set(storage.__all__)


@pytest.mark.parametrize("hit, cur_sig, expected", [
    (_FRESH, "s1", True),
    (_STALE, "s1", True),        # ПРОТУХЛО, но версия та же -> описание переиспользуемо
    (_FRESH, "s2", False),       # вакансию переписали -> описание не про неё
    (_EMPTY, "s1", False),
    (None, "s1", False),
])
def test_cache_hit_matches_ignores_age(hit, cur_sig, expected):
    assert cache_hit_matches(hit, cur_sig) is expected


@pytest.mark.parametrize("hit, cur_sig, expected", [
    (_FRESH, "s1", True),
    (_STALE, "s1", False),       # старше DESC_CACHE_MAX_AGE_DAYS=14 -> пере-обогатить
    (_FRESH, "s2", False),
    (_EMPTY, "s1", False),
    (None, "s1", False),
    ({"sig": "s1", "description_html": "<p>x</p>"}, "s1", True),   # legacy без 'at' -> не тухнет
])
def test_cache_hit_usable_adds_the_age_guard(hit, cur_sig, expected):
    assert cache_hit_usable(hit, cur_sig) is expected
