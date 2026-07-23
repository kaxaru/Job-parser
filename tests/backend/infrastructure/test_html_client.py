"""Тесты дозагрузки карточек HH: сбой сети не затирает описания; инкремент по кешу."""
import asyncio

from hrwork.domain.parsing import build_vacancy
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.sources.hh import HHHtmlClient, _record_from_search_item
from hrwork.infrastructure.storage import VacancyRecord


def _rec(vid="1", req="старый текст", desc="<p>старое описание</p>", sig=""):
    v = build_vacancy(vid=vid, name="Dev", city="", city_id="", salary=None,
                      experience=None, schedule=Schedule.OFFICE, detect_text="Dev",
                      employer="", created_at=None, published_at=None, responses=None,
                      source="hh")
    return VacancyRecord(vacancy=v, requirement=req, description_html=desc, sig=sig)


def test_enrich_failure_keeps_existing(monkeypatch):
    client = HHHtmlClient()
    rec = _rec()

    async def fail(vac_id):
        return "", ""

    monkeypatch.setattr(client, "_fetch_detail", fail)
    asyncio.run(client._enrich(rec))
    assert rec.requirement == "старый текст"
    assert rec.description_html == "<p>старое описание</p>"


def test_enrich_success_overwrites(monkeypatch):
    client = HHHtmlClient()
    rec = _rec()

    async def ok(vac_id):
        return "python docker", "<p>новое</p>"

    monkeypatch.setattr(client, "_fetch_detail", ok)
    asyncio.run(client._enrich(rec))
    assert rec.requirement == "python docker"
    assert rec.description_html == "<p>новое</p>"
    assert rec.enriched is True                       # успех помечает запись обогащённой
    assert "Python" in rec.vacancy.techs              # техи пере-собраны из текста карточки


def test_enrich_uses_cache_and_fetches_only_changed(monkeypatch):
    client = HHHtmlClient()
    # id1 неизменён (sig совпадает с кешем) -> из кеша; id2 переоткрыт (sig другой) -> дозагрузка
    r1 = _rec(vid="1", req="", desc="", sig="t1")
    r2 = _rec(vid="2", req="", desc="", sig="t2-new")

    monkeypatch.setattr(storage, "load_desc_cache", lambda: {
        "1": {"sig": "t1", "description_html": "<p>cached 1</p>", "requirement": "java"},
        "2": {"sig": "t2-old", "description_html": "<p>stale 2</p>", "requirement": "go"},
    })
    fetched = []

    async def ok(vac_id):
        fetched.append(vac_id)
        return "python", f"<p>fresh {vac_id}</p>"

    monkeypatch.setattr(client, "_fetch_detail", ok)
    asyncio.run(client.run_enrich([r1, r2]))

    assert fetched == ["2"]                            # неизменную (id1) НЕ тянем
    assert r1.description_html == "<p>cached 1</p>"    # из кеша
    assert r1.requirement == "java"                   # текст карточки тоже восстановлен
    assert r1.enriched is True
    assert "Java" in r1.vacancy.techs                 # техи пере-собраны из кешированного текста
    assert r2.description_html == "<p>fresh 2</p>"     # переоткрытую дозагрузили
    assert r2.enriched is True


def test_record_from_search_item_sets_sig_and_domain():
    item = {"vacancyId": 42, "name": "Python Dev", "creationTime": "2026-05-01T00:00:00+03:00",
            "publicationTime": {"$": "2026-05-02T00:00:00+03:00"},
            "workExperience": "between1And3",
            "workFormats": [{"workFormatsElement": ["REMOTE"]}]}
    r = _record_from_search_item(item, city="Москва", city_id="1")
    assert r.sig == "2026-05-02T00:00:00+03:00"       # publicationTime приоритетнее
    assert r.enriched is False                        # до enrich описания нет
    v = r.vacancy
    assert v.id == "42" and v.city == "Москва"        # город — из поиска, не из area
    assert v.schedule.hh_code == "remote"
    assert v.experience.hh_id == "between1And3"


def test_record_salary_zero_bound_and_missing():
    base = {"vacancyId": 1, "name": "Dev"}
    # from=0 — валидная граница, не «нет вилки» (is None, не truthiness — как Salary.from_raw)
    z = _record_from_search_item({**base, "compensation": {"from": 0, "to": 100_000,
                                                          "currencyCode": "RUR"}},
                                 city="М", city_id="1")
    assert z.vacancy.salary is not None and z.vacancy.salary.mid == 50_000
    # вилки нет вовсе -> None, а не Salary-шелуха
    n = _record_from_search_item({**base, "compensation": {"currencyCode": "RUR"}},
                                 city="М", city_id="1")
    assert n.vacancy.salary is None
