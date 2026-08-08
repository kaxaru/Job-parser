"""Схема `cache_meta.json`: кто владеет базой сравнения санити-гейта.

`save_meta` собирает meta с нуля и пишет её ЦЕЛИКОМ, поэтому «объёмы источников до
кросс-портального дедупа» обязаны быть её собственным параметром, а не ключом, который
дописывает поверх кто-то снаружи. Дописывание снаружи держалось на порядке вызовов, и любой
второй писатель (`hh.py::enrich` зовёт `repo.save` тоже) стирал базу молча: следующий сбор
съезжал на мягкий post-dedup фолбэк, а заметить это было нечем.

Контракт, который здесь закреплён:
  * значение передано -> оно и лежит в файле;
  * значение не передано (None) -> прошлая база переносится из существующего файла;
  * базы нет или она битая -> ключа в файле нет, и читатель отдаёт пусто, а не мусор.
"""
import json
from pathlib import Path

import pytest

from hrwork.domain.models import Vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.storage import VacancyRecord, files
from hrwork.infrastructure.storage.repository import JsonVacancyRepository

# Имя ключа — контракт с файлом на диске: в тесте оно ЛИТЕРАЛ, а не files.PRE_DEDUP_META_KEY.
# Переименование ломает совместимость с уже лежащим cache_meta.json, и тест обязан упасть.
META_KEY = "pre_dedup_by_source"


@pytest.fixture
def meta_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cache_meta.json"
    monkeypatch.setattr(files, "META_FILE", path)
    return path


def _meta(path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _rec(i: int) -> VacancyRecord:
    return VacancyRecord(vacancy=Vacancy(
        id=f"talanto_{i}", name=f"Python Developer {i}", city="Москва", city_id="1",
        salary=None, experience=None, schedule=Schedule.REMOTE,
        employer=f"Company {i}", source="talanto"))


# ── save_meta владеет ключом ────────────────────────────────────────────────────────────

def test_given_volumes_are_written_as_the_gate_base(meta_file):
    files.save_meta(800, {"talanto": 1000, "hh": 30000})
    assert _meta(meta_file)[META_KEY] == {"talanto": 1000, "hh": 30000}


def test_count_is_written_next_to_the_base(meta_file):
    files.save_meta(800, {"talanto": 1000})
    assert _meta(meta_file)["count"] == 800


def test_omitted_volumes_keep_the_base_already_on_disk(meta_file):
    # Это путь `hh.py::enrich`: состав источников не менялся, дозагрузка карточек не имеет
    # права стереть базу — иначе следующий сбор сравнивает pre-dedup с post-dedup.
    files.save_meta(800, {"talanto": 1000})
    files.save_meta(800)
    assert _meta(meta_file)[META_KEY] == {"talanto": 1000}


def test_new_volumes_replace_the_previous_base(meta_file):
    files.save_meta(800, {"talanto": 1000, "web3": 957})
    files.save_meta(700, {"talanto": 900})          # web3 выключен -> уходит и из базы
    assert _meta(meta_file)[META_KEY] == {"talanto": 900}


def test_meta_without_a_base_gets_no_empty_key(meta_file):
    # Старый cache_meta.json без ключа — норма, мигрировать нечего: пустой ключ-пустышка
    # только сбивал бы читателя («база есть, но нулевая»).
    files.save_meta(800)
    assert META_KEY not in _meta(meta_file)


# ── чтение базы: мягкий контракт на внешнем файле ───────────────────────────────────────

def test_stored_base_reads_back_as_counts(meta_file):
    files.save_meta(800, {"talanto": 1000, "hh": 30000})
    assert files.load_pre_dedup_counts() == {"talanto": 1000, "hh": 30000}


def test_missing_meta_file_reads_as_no_base(meta_file):
    assert files.load_pre_dedup_counts() == {}


@pytest.mark.parametrize("stored", [
    {"count": 800},                                  # meta от версии без ключа
    {"count": 800, META_KEY: {}},                    # ключ есть, но пустой
    {"count": 800, META_KEY: {"talanto": "1000"}},   # число строкой (правка руками)
    {"count": 800, META_KEY: {"talanto": -5}},       # отрицательный счётчик
    {"count": 800, META_KEY: {"talanto": True}},     # bool — не счётчик, хоть и int
    {"count": 800, META_KEY: {"talanto": 1.5}},      # дробный счётчик
    {"count": 800, META_KEY: ["talanto", 1000]},     # не тот тип целиком
    {"count": 800, META_KEY: None},
])
def test_a_base_of_the_wrong_shape_reads_as_no_base(meta_file, stored):
    # Файл на диске правят руками и он переживает апдейты, поэтому любое отклонение — не
    # падение, а «базы нет»: вызывающий берёт свой фолбэк (post-dedup счётчики кеша).
    meta_file.write_text(json.dumps(stored), encoding="utf-8")
    assert files.load_pre_dedup_counts() == {}


def test_a_single_broken_entry_discards_the_whole_base(meta_file):
    # Половина базы — хуже отсутствия базы: гейт судил бы часть источников по реальному
    # порогу, а часть по мягкому фолбэку, и понять по логу, что именно случилось, нельзя.
    meta_file.write_text(json.dumps({META_KEY: {"hh": 30000, "talanto": "1000"}}),
                         encoding="utf-8")
    assert files.load_pre_dedup_counts() == {}


# ── репозиторий пробрасывает базу, не зная про схему meta ───────────────────────────────

def test_repository_save_passes_the_volumes_through(tmp_path, meta_file):
    repo = JsonVacancyRepository(tmp_path / "vacancies_raw.json")
    repo.save([_rec(1), _rec(2)], pre_dedup_by_source={"talanto": 1000})
    assert _meta(meta_file)[META_KEY] == {"talanto": 1000}


def test_repository_save_without_volumes_keeps_the_base(tmp_path, meta_file):
    # Обратная совместимость вызывающих: `repo.save(records)` остаётся валидным вызовом
    # и не стирает базу гейта.
    repo = JsonVacancyRepository(tmp_path / "vacancies_raw.json")
    repo.save([_rec(1)], pre_dedup_by_source={"talanto": 1000})
    repo.save([_rec(1), _rec(2)])
    after = _meta(meta_file)
    assert (after[META_KEY], after["count"]) == ({"talanto": 1000}, 2)
