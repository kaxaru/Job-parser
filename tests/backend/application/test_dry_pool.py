"""`hh.py autoclick --dry-pool`: срез пула до первого отклика (RFC-004).

Отклик необратим, поэтому отбор утверждается человеком ПО СРЕЗУ: сколько вакансий, каких
ярусов и ролей, что съело пул и какие написания работодателя отбил блок-лист. Работодатели в
тестах вымышленные — репозиторий публичный.
"""
import pytest

from hrwork.application.apply import candidates, dry_pool
from hrwork.application.apply.ab_split import PoolInputs
from hrwork.infrastructure.storage import JsonVacancyRepository

pytestmark = pytest.mark.usefixtures("apply_defaults")


def _hh(vid: str, name: str = "Python Backend разработчик", employer: str = "Acme",
        req: str = "FastAPI, Docker, удалённая работа", source: str = "hh"):
    return JsonVacancyRepository._from_dict({
        "id": vid, "name": name, "area": {"id": "1", "name": "Москва"}, "salary": None,
        "experience": {"id": "between1And3"}, "schedule": {"id": "remote"},
        "snippet": {"requirement": req, "responsibility": ""},
        "alternate_url": f"https://hh.ru/vacancy/{vid}", "employer": {"name": employer},
        "created_at": None, "_source": source,
    })


@pytest.fixture
def blocklist(monkeypatch):
    monkeypatch.setattr(candidates, "APPLY_EMPLOYER_BLOCK", candidates._employer_rx(["K7"]))


@pytest.fixture
def records():
    return [
        _hh("1"),
        _hh("2", employer="K7 Tech"),
        _hh("3", employer="К7 Медиа"),                   # кириллическая «К»
        _hh("4", employer="K7, Управляющая компания"),         # уже отмечена — но в списке написаний
        _hh("5", name="Тестировщик"),
        _hh("6", source="getmatch"),
        _hh("7", name="Python-разработчик", employer="ООО Ромашка"),
    ]


def test_report_counts_pool_and_names_blocked_spellings(records, blocklist):
    report = dry_pool.build_report(records, PoolInputs(marks={"4": "rejected"}))
    assert [c.id for c in report.pool] == ["1", "7"]
    assert report.tiers == {"STRICT": 2}
    assert report.rejected == {"работодатель в блок-листе": 2, "не HH": 1,
                               "тайтл вне специализации": 1, "уже отмечена": 1}
    assert report.blocked_employers == {"K7 Tech": 1, "K7, Управляющая компания": 1,
                                        "К7 Медиа": 1}
    assert report.employers == [("Acme", 1), ("ООО Ромашка", 1)]
    assert report.blocklist_active is True


def test_format_distinguishes_empty_blocklist_from_no_matches(records):
    lines = dry_pool.format_report(dry_pool.build_report(records, PoolInputs(marks={})))
    assert lines[0] == "Пул кандидатов (dry-pool) [main]: 5"                # без блок-листа K7 в пуле
    assert lines[-1] == "  блок-лист работодателей: пуст (APPLY_EMPLOYER_BLOCKLIST в .env не задан)"


def test_format_says_no_matches_when_blocklist_misses_the_cache(monkeypatch):
    monkeypatch.setattr(candidates, "APPLY_EMPLOYER_BLOCK", candidates._employer_rx(["Нет-такой"]))
    lines = dry_pool.format_report(dry_pool.build_report([_hh("1")], PoolInputs(marks={})))
    assert lines == [
        "Пул кандидатов (dry-pool) [main]: 1",
        "  ярусы: STRICT 1",
        "  роли: Backend 1",
        "  топ работодателей: Acme 1",
        "  топ тайтлов: Python Backend разработчик 1",
        "  отсев: -",
        "  блок-лист работодателей: совпадений в кеше нет",
    ]
