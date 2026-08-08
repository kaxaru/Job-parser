"""Санити-гейт сбора: деградированный срез источника не затирает полный кеш.

Два договора гейта, которые проверяются здесь:

1. Гейт судит только ВКЛЮЧЁННЫЕ источники (`config.SOURCES`). Портал, выключенный нами,
   не считается просевшим — иначе его выключение заперло бы запись кеша навсегда. Портал,
   который включён, а данных не дал (протух токен), просевшим считается.
2. Гейт сравнивает pre-dedup объём с pre-dedup объёмом. База хранится в `cache_meta.json`
   под ключом `pre_dedup_by_source`; кеша со старой meta (ключа нет) хватает на мягкий
   фолбэк — блокировки он не даёт, а правильную базу записывает по итогам прогона.

Пороги спецификации (`COLLECT_MIN_RATIO` 0.5, `COLLECT_SANITY_MIN` 500) прибиты фикстурой
на ВЕСЬ модуль: иначе `.env` разработчика менял бы ожидаемые значения. До 09.08.2026 это
распространялось только на оркестрационные случаи, а юнит-половина строила вход из тех же
env-настраиваемых констант, с которыми сравнивает реализация — граничный тест был зелёным
при любом пороге, а `COLLECT_SANITY_MIN=1` превращал «малый источник» в проверку 0 против 0.
"""
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import hh
from hrwork.domain.models import Vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.storage import VacancyRecord, atomic_write_json, files

# Ключ meta — контракт с файлом на диске, поэтому в тестах он ЛИТЕРАЛ, а не
# files.PRE_DEDUP_META_KEY: переименование ключа ломает совместимость с уже лежащим
# cache_meta.json, и тест обязан упасть.
META_KEY = "pre_dedup_by_source"


@pytest.fixture(autouse=True)
def spec_thresholds(monkeypatch):
    """Пороги СПЕЦИФИКАЦИИ у держателя, через которого идёт вызов (`hh`), — не значения
    `.env` запускающего. Числа записаны литералом: сравнивать вход с той же константой,
    с которой сравнивает реализация, значит не проверять порог вообще."""
    monkeypatch.setattr(hh, "COLLECT_MIN_RATIO", 0.5)
    monkeypatch.setattr(hh, "COLLECT_SANITY_MIN", 500)


# ─── _degraded_source: чистая функция гейта ───────────────────────────────────

def test_degraded_detects_source_collapse():
    prior = Counter({"hh": 30000, "hirify": 18000})
    # hh рухнул вдвое (блок), hirify цел -> флагим hh
    now = Counter({"hh": 12000, "hirify": 18000})
    assert hh._degraded_source(now, prior, ["hh", "hirify"]) == "hh"


def test_degraded_ok_when_stable_or_grows():
    prior = Counter({"hh": 30000, "hirify": 18000})
    now = Counter({"hh": 29500, "hirify": 19000})       # норм колебание/рост
    assert hh._degraded_source(now, prior, ["hh", "hirify"]) is None


def test_degraded_ignores_small_sources():
    # маленький источник ниже порога значимости (500) — не флагим (шум)
    prior = Counter({"tiny": 499})
    now = Counter({"tiny": 0})
    assert hh._degraded_source(now, prior, ["tiny"]) is None


def test_degraded_watches_a_source_at_the_significance_threshold():
    # ровно на пороге значимости источник уже под наблюдением: 249 < 500 * 0.5
    prior = Counter({"tiny": 500})
    now = Counter({"tiny": 249})
    assert hh._degraded_source(now, prior, ["tiny"]) == "tiny"


@pytest.mark.parametrize("collected, degraded", [
    (499, "hh"),      # ниже половины прошлого объёма -> просадка портала
    (500, None),      # ровно половина — порог не перейдён (сравнение строгое)
    (501, None),
])
def test_degraded_threshold_boundary(collected, degraded):
    assert hh._degraded_source(Counter({"hh": collected}),
                               Counter({"hh": 1000}), ["hh"]) == degraded


def test_source_removed_from_sources_is_not_a_collapse():
    # БАГ 08.08.2026: web3 убрали из SOURCES -> его ноль читался как блок портала,
    # запись кеша блокировалась КАЖДЫЙ прогон (meta пишется только после repo.save),
    # и сбор часами повторялся вхолостую до ручного --force.
    prior = Counter({"hh": 30000, "web3": 957})
    now = Counter({"hh": 30000})
    assert hh._degraded_source(now, prior, ["hh"]) is None


def test_enabled_source_with_empty_result_is_a_collapse():
    # Обратная половина того же сценария: web3 в SOURCES ЕСТЬ, а токен протух и портал
    # молча отдал [] — это ровно та поломка, ради которой гейт существует.
    prior = Counter({"hh": 30000, "web3": 957})
    now = Counter({"hh": 30000})
    assert hh._degraded_source(now, prior, ["hh", "web3"]) == "web3"


# ─── Оркестрация collect(): стаб-источники, стаб-репозиторий, meta в tmp_path ──

class _StubSource:
    """Портал без сети: отдаёт заранее заданный список и считает вызовы."""

    def __init__(self, records: list[VacancyRecord]) -> None:
        self._records = records
        self.calls = 0

    async def collect(self) -> list[VacancyRecord]:
        self.calls += 1
        return list(self._records)


class _StubRepo:
    """Кеш в памяти, но meta — НАСТОЯЩАЯ: save() зовёт `storage/files.py::save_meta`, как
    делает `JsonVacancyRepository.save`. Подменять запись meta нельзя, иначе тест проверял
    бы заглушку: именно save_meta переписывает cache_meta.json целиком и владеет ключом
    базы санити-гейта."""

    def __init__(self, records: list[VacancyRecord]) -> None:
        self._records = list(records)
        self.saved: list[VacancyRecord] | None = None

    def exists(self) -> bool:
        return bool(self._records)

    def load(self) -> list[VacancyRecord]:
        return list(self._records)

    def save(self, records: list[VacancyRecord], *,
             pre_dedup_by_source: dict[str, int] | None = None) -> None:
        self.saved = list(records)
        self._records = list(records)
        files.save_meta(len(records), pre_dedup_by_source)


class _NoProxies:
    enabled = False
    size = 0


class _StubEnrichClient:
    """HHHtmlClient без браузера и сети: описания уже на месте."""

    def __init__(self, proxies: list[str] | None = None) -> None:
        self.proxies = proxies

    async def run_enrich(self, records: list[VacancyRecord], skip_filled: bool = False) -> None:
        return None


def _rec(source: str, i: int) -> VacancyRecord:
    """Запись кеша: гейту важны только источник и уникальность (id, работодатель+тайтл)."""
    return VacancyRecord(vacancy=Vacancy(
        id=f"{source}-{i}", name=f"Python Developer {i}", city="Москва", city_id="1",
        salary=None, experience=None, schedule=Schedule.REMOTE,
        employer=f"Company {source} {i}", source=source))


def _twin(source: str, i: int) -> VacancyRecord:
    """Запись, которую два портала опубликовали дословно: работодатель+тайтл совпадают,
    поэтому `dedup_cross_source` снимет копию с младшего по приоритету источника."""
    return VacancyRecord(vacancy=Vacancy(
        id=f"{source}-{i}", name=f"Python Developer {i}", city="Москва", city_id="1",
        salary=None, experience=None, schedule=Schedule.REMOTE,
        employer=f"Acme {i}", source=source))


def _batch(source: str, n: int, start: int = 0) -> list[VacancyRecord]:
    return [_rec(source, start + i) for i in range(n)]


def _meta(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _setup(tmp_path, monkeypatch, *, sources: list[str],
           cache: list[VacancyRecord], today: dict[str, list[VacancyRecord]],
           meta: dict[str, Any], cache_fresh: bool = False) -> tuple[_StubRepo, Path]:
    meta_path = tmp_path / "cache_meta.json"
    atomic_write_json(meta_path, meta, indent=2)
    repo = _StubRepo(cache)
    stubs = {name: _StubSource(today.get(name, [])) for name in sources}
    # META_FILE подменяется у ВЛАДЕЛЬЦА схемы (storage/files.py): и запись, и чтение базы
    # гейта идут теперь только оттуда, оркестратор meta не трогает.
    monkeypatch.setattr(files, "META_FILE", meta_path)
    monkeypatch.setattr(hh, "SOURCES", list(sources))      # пороги — в фикстуре spec_thresholds
    monkeypatch.setattr(hh, "JsonVacancyRepository", lambda *a, **k: repo)
    monkeypatch.setattr(hh, "load_proxies", lambda *a, **k: _NoProxies())
    monkeypatch.setattr(hh, "get_source", lambda name, proxies=(): stubs.get(name))
    monkeypatch.setattr(hh.storage, "cache_valid", lambda force=False: cache_fresh and not force)
    return repo, meta_path


def test_collect_blocks_write_when_source_halves_against_pre_dedup_base(tmp_path, monkeypatch):
    # РЕГРЕССИЯ 08.08.2026: базой сравнения был POST-dedup файл (собрано 1000 -> сохранено 800),
    # поэтому сегодняшние 420 читались как «52 % от 800» и проходили гейт, хотя реальная
    # просадка портала — 58 %.
    repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["talanto"],
        cache=_batch("talanto", 800),                                  # post-dedup срез в файле
        today={"talanto": _batch("talanto", 420, start=10_000)},
        meta={"count": 800, META_KEY: {"talanto": 1000}})

    result = asyncio.run(hh.collect(force=False))

    assert repo.saved is None                              # 420 < 1000 * 0.5 -> кеш не трогаем
    assert len(result) == 800                              # вернулся прошлый срез целиком
    assert _meta(meta_path)[META_KEY] == {"talanto": 1000}   # база сравнения не сдвинулась


@pytest.mark.parametrize("stored_meta", [
    {"count": 800},                              # meta от версии без ключа
    {"count": 800, "pre_dedup_by_source": {}},   # ключ есть, но пустой
    {"count": 800, "pre_dedup_by_source": {"talanto": "1000"}},   # битый тип значения
    {"count": 800, "pre_dedup_by_source": {"talanto": -5}},       # отрицательный счётчик
])
def test_collect_writes_pre_dedup_base_when_meta_has_none(tmp_path, monkeypatch, stored_meta):
    # Первый прогон после апдейта: сравнивать не с чем, поэтому гейт НЕ блокирует
    # (мягкий фолбэк на post-dedup кеш: 420 > 800 * 0.5), а базу записывает.
    repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["talanto"],
        cache=_batch("talanto", 800),
        today={"talanto": _batch("talanto", 420, start=10_000)},
        meta=stored_meta)

    result = asyncio.run(hh.collect(force=False))

    assert len(result) == 420
    assert repo.saved is not None
    assert len(repo.saved) == 420
    after = _meta(meta_path)
    assert after[META_KEY] == {"talanto": 420}     # база записана по СЕГОДНЯШНЕМУ pre-dedup
    assert after["count"] == 420                   # и дописана поверх свежей meta, а не вместо неё


def test_stored_base_holds_pre_dedup_volumes_not_what_survived_dedup(tmp_path, monkeypatch):
    # НАХОДКА 17 (вторая половина): база сравнения обязана хранить то, что портал РЕАЛЬНО
    # отдал. talanto переопубликовывает вакансии hh дословно, и кросс-портальный дедуп
    # снимает копии уже после подсчёта by_src; запиши мы post-dedup число, завтрашний гейт
    # снова сравнивал бы разное с разным — ровно тот дефект, который закрывался.
    _repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["hh", "talanto"],
        cache=[],                                       # пустой кеш -> гейту сравнивать не с чем
        today={"hh": [_twin("hh", i) for i in range(3)],
               "talanto": [_twin("talanto", i) for i in range(3)] + _batch("talanto", 1, start=90)},
        meta={})

    result = asyncio.run(hh.collect(force=False))

    assert len(result) == 4                             # три копии talanto схлопнулись в пользу hh
    assert _meta(meta_path)[META_KEY] == {"hh": 3, "talanto": 4}


def test_collect_writes_cache_after_source_left_sources(tmp_path, monkeypatch):
    # БАГ 08.08.2026: web3 убран из SOURCES -> его 957 в базе валили гейт каждый прогон.
    repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["hh"],
        cache=_batch("hh", 900) + _batch("web3", 957),
        today={"hh": _batch("hh", 1000, start=10_000)},
        meta={"count": 1857, META_KEY: {"hh": 1000, "web3": 957}})

    result = asyncio.run(hh.collect(force=False))

    assert len(result) == 1000
    assert repo.saved is not None
    assert len(repo.saved) == 1000
    assert _meta(meta_path)[META_KEY] == {"hh": 1000}   # выключенный портал ушёл и из базы


def test_collect_blocks_write_when_enabled_source_returns_nothing(tmp_path, monkeypatch):
    # web3 в SOURCES остался, а токен протух -> портал молча отдал []: это блок, не решение.
    repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["hh", "web3"],
        cache=_batch("hh", 900) + _batch("web3", 957),
        today={"hh": _batch("hh", 1000, start=10_000), "web3": []},
        meta={"count": 1857, META_KEY: {"hh": 1000, "web3": 957}})

    result = asyncio.run(hh.collect(force=False))

    assert repo.saved is None
    assert len(result) == 1857
    assert _meta(meta_path)[META_KEY] == {"hh": 1000, "web3": 957}


def test_fresh_cache_short_circuits_collect_without_force(tmp_path, monkeypatch):
    repo, _ = _setup(
        tmp_path, monkeypatch,
        sources=["hh"],
        cache=_batch("hh", 900),
        today={"hh": _batch("hh", 100, start=10_000)},
        meta={"count": 900, META_KEY: {"hh": 1000}},
        cache_fresh=True)

    result = asyncio.run(hh.collect(force=False))

    assert len(result) == 900        # отдан кеш, сбор не запускался
    assert repo.saved is None


def test_force_overwrites_cache_despite_collapse_and_rebases_gate(tmp_path, monkeypatch):
    # Аварийный выход: --force снимает ОБА уровня защиты — и свежий кеш, и санити-гейт.
    repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["hh"],
        cache=_batch("hh", 900),
        today={"hh": _batch("hh", 100, start=10_000)},      # 100 << 1000 * 0.5
        meta={"count": 900, META_KEY: {"hh": 1000}},
        cache_fresh=True)

    result = asyncio.run(hh.collect(force=True))

    assert len(result) == 100
    assert repo.saved is not None
    assert len(repo.saved) == 100
    assert _meta(meta_path)[META_KEY] == {"hh": 100}   # новая база — то, что реально собрано


def test_enrich_keeps_pre_dedup_base(tmp_path, monkeypatch):
    # Дозагрузка карточек тоже зовёт repo.save, а save_meta переписывает meta целиком.
    # Без переноса ключа enrich стирал бы базу гейта, и следующий сбор молча съезжал
    # на мягкий post-dedup фолбэк.
    repo, meta_path = _setup(
        tmp_path, monkeypatch,
        sources=["talanto"],
        cache=_batch("talanto", 800),
        today={},
        meta={"count": 800, META_KEY: {"talanto": 1000}})
    monkeypatch.setattr(hh, "HHHtmlClient", _StubEnrichClient)

    asyncio.run(hh.enrich(only_empty=False))

    after = _meta(meta_path)
    assert after[META_KEY] == {"talanto": 1000}
    assert after["count"] == 800
    assert repo.saved is not None
    assert len(repo.saved) == 800
