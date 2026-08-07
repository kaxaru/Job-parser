"""Тесты источника hirify: ACL hirify-API -> VacancyRecord (домен напрямую, без сети)."""
import asyncio

from hrwork.infrastructure import sources, storage
from hrwork.infrastructure.sources.hirify import (
    HirifyCfg,
    HirifySource,
    _clean_company,
    _normalize,
)

ITEM = {
    "id": 712795, "slug": "712795-python-developer-middlejunior",
    "title": "Python Developer (Middle/Junior)", "company_title": "Torc",
    "work_format": ["remote"],
    "salary": {"min": 600, "max": 700, "currency": "USD"},
    "grades": [{"name": "junior"}, {"name": "middle"}],
    "specializations": [{"code": "backend_dev", "name_en": "Backend"}],
    "tags": [{"name": "python"}, {"name": "fullstack"}],
    "regions": [{"name_en": "United Kingdom"}],
    "created_at": "2026-07-06T07:26:02.000000Z",
}


def test_normalize_maps_to_domain():
    r = _normalize(ITEM)
    v = r.vacancy
    assert v.id == "hirify_712795"                       # неймспейс — не столкнётся с id HH
    assert v.name == "Python Developer (Middle/Junior)"
    assert v.source == "hirify"
    assert r.url == "https://hirify.me/jobs/712795-python-developer-middlejunior"
    assert v.employer == "Torc"
    assert v.city == "United Kingdom"
    assert v.schedule.hh_code == "remote"
    assert (v.salary.frm, v.salary.to, v.salary.currency) == (600, 700, "USD")
    assert v.experience.hh_id == "between1And3"          # junior — самый младший грейд
    # теги/спец подмешаны в requirement -> детект стека/роли сработал прямо в адаптере
    assert "python" in r.requirement.lower() and "Backend" in r.requirement
    assert "Python" in v.techs and v.role.is_it


def test_normalize_salary_dict_without_range_is_none():
    # словарь зарплаты без min/max (одна валюта) -> None, а не truthy Salary(None, None, …)
    it = {"id": 2, "slug": "y", "title": "T", "grades": [], "work_format": [],
          "salary": {"currency": "USD"}}
    assert _normalize(it).vacancy.salary is None


def test_normalize_no_salary_no_region_defaults_remote():
    r = _normalize({"id": 1, "slug": "x", "title": "T", "grades": [], "work_format": []})
    v = r.vacancy
    assert v.salary is None
    assert v.city == "Remote"
    assert v.schedule.hh_code == "fullDay"
    assert v.id == "hirify_1"
    assert v.experience is None                          # грейдов нет -> опыт не указан


def test_sources_registry_has_both_portals():
    assert sources.get_source("hh") is not None
    assert sources.get_source("hirify") is not None
    assert sources.get_source("unknown") is None


def test_clean_company_drops_placeholders():
    assert _clean_company("%hirify_global%") == ""     # плейсхолдер -> пусто
    assert _clean_company(None) == ""
    assert _clean_company("") == ""
    assert _clean_company("Wildberries") == "Wildberries"


def test_normalize_meta_header_and_full_description():
    it = {**ITEM, "english_level": "b2",
          "salary": {"min": 600, "max": 700, "currency": "USD", "salary_in_usd": 650}}
    d = _normalize(it, {"text": "<p>Big job description</p>"}).description_html
    assert "United Kingdom" in d          # страна-наниматель
    assert "English B2" in d              # минимальный уровень английского
    assert "650" in d                     # нормализованный USD (мета)
    assert "Big job description" in d     # полный текст из /vacancies/{slug}
    assert "hirify.me" in d


def test_normalize_placeholder_company_cleaned():
    assert _normalize({**ITEM, "company_title": "%hirify_global%"}).vacancy.employer == ""


# Инференс периода и пересчёт в месячную переехали в домен (SalaryPeriod) — их тесты
# теперь в tests/backend/domain/test_salary_period.py. Здесь остаётся то, что относится
# к САМОМУ адаптеру: что он достаёт USD-величину и передаёт её домену.
def test_hirify_reads_usd_mid_for_period_inference():
    from hrwork.infrastructure.sources.hirify import _usd_mid
    assert _usd_mid({"salary_in_usd": 72000}) == 72000.0
    assert _usd_mid({"salary_in_usd": None}) is None
    assert _usd_mid({}) is None
    assert _usd_mid(None) is None
    assert _usd_mid({"salary_in_usd": "мусор"}) is None


def test_normalize_period_year_to_monthly():
    it = {**ITEM, "salary": {"min": 60000, "max": 84000, "currency": "USD", "salary_in_usd": 72000}}
    v = _normalize(it).vacancy
    assert v.salary.frm == 5000 and v.salary.to == 7000   # годовая /12 -> месяц


def test_normalize_sig_and_enriched_flag():
    # tldr-заглушка -> enriched False, sig из created_at (updated_at нет)
    r = _normalize(ITEM)
    assert r.enriched is False
    assert r.sig == ITEM["created_at"]
    # updated_at приоритетнее created_at
    assert _normalize({**ITEM, "updated_at": "2026-07-07T10:00:00Z"}).sig == "2026-07-07T10:00:00Z"
    # полный текст -> enriched True
    r3 = _normalize(ITEM, {"text": "<p>full</p>"})
    assert r3.enriched is True and "full" in r3.description_html
    # описание из кеша -> enriched True, html как есть, сеть не нужна
    r4 = _normalize(ITEM, cached_desc="<p>cached</p>")
    assert r4.enriched is True and r4.description_html == "<p>cached</p>"


def test_collect_reuses_cache_and_enriches_only_changed(monkeypatch):
    items = [
        {"id": 1, "slug": "a", "title": "A", "updated_at": "2026-07-01T00:00:00Z",
         "created_at": "2026-07-01T00:00:00Z", "grades": [], "work_format": []},
        {"id": 2, "slug": "b", "title": "B", "updated_at": "2026-07-02T00:00:00Z",
         "created_at": "2026-07-02T00:00:00Z", "grades": [], "work_format": []},
    ]
    src = HirifySource()

    async def fake_page(page):
        return {"data": items, "last_page": 1, "total": len(items)}

    fetched: list[str] = []

    async def fake_one(slug):
        fetched.append(slug)
        return {"text": f"<p>full {slug}</p>"}

    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    # id 1 уже в кеше с совпадающим sig -> reuse; id 2 новый -> дозагрузка
    monkeypatch.setattr(storage, "load_desc_cache",
                        lambda: {"hirify_1": {"sig": "2026-07-01T00:00:00Z",
                                              "description_html": "<p>cached A</p>",
                                              "requirement": ""}})

    out = asyncio.run(src.collect())
    assert fetched == ["b"]                                   # неизменную (id1) НЕ тянем
    by = {r.vacancy.id: r for r in out}
    assert by["hirify_1"].description_html == "<p>cached A</p>"  # из кеша
    assert by["hirify_1"].enriched is True
    assert "full b" in by["hirify_2"].description_html       # id2 дозагружен
    assert by["hirify_2"].enriched is True


def test_cache_hit_usable_respects_max_age():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)
    fresh = {"sig": "s", "description_html": "<p>x</p>", "at": (now - timedelta(days=1)).isoformat()}
    stale = {"sig": "s", "description_html": "<p>x</p>", "at": (now - timedelta(days=20)).isoformat()}
    assert storage.cache_hit_usable(fresh, "s") is True
    assert storage.cache_hit_usable(stale, "s") is False       # старше 14 дней -> пере-обогатить
    assert storage.cache_hit_usable(fresh, "other") is False   # сигнал не совпал
    assert storage.cache_hit_usable(None, "s") is False
    assert storage.cache_hit_usable({"sig": "s", "description_html": ""}, "s") is False  # нет описания
    no_at = {"sig": "s", "description_html": "<p>x</p>"}        # legacy без 'at' -> не тухнет по времени
    assert storage.cache_hit_usable(no_at, "s") is True


def test_stale_cache_triggers_refetch(monkeypatch):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()
    it = {"id": 9, "slug": "z", "title": "Z", "updated_at": "2026-07-01T00:00:00Z",
          "created_at": "2026-07-01T00:00:00Z", "grades": [], "work_format": []}
    src = HirifySource()

    async def fake_page(page):
        return {"data": [it], "last_page": 1, "total": 1}

    fetched: list[str] = []

    async def fake_one(slug):
        fetched.append(slug)
        return {"text": "<p>fresh</p>"}

    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    # sig совпадает, НО запись старше 14 дней -> должна пере-обогатиться
    monkeypatch.setattr(storage, "load_desc_cache", lambda: {
        "hirify_9": {"sig": "2026-07-01T00:00:00Z", "description_html": "<p>old</p>",
                     "requirement": "", "at": old}})
    out = asyncio.run(src.collect())
    assert fetched == ["z"]                                     # протухла -> дозагрузка
    assert "fresh" in out[0].description_html


def test_collect_caps_enrich_at_limit(monkeypatch):
    # объём > HIRIFY_ENRICH_MAX: за прогон тянем не больше лимита, остальное -> tldr
    monkeypatch.setattr("hrwork.infrastructure.sources.hirify.CFG", HirifyCfg(enrich_max=1))
    items = [
        {"id": i, "slug": f"s{i}", "title": f"T{i}",
         "created_at": f"2026-07-0{i}T00:00:00Z", "grades": [], "work_format": [],
         "tldr": f"tldr {i}"}
        for i in (1, 2, 3)
    ]

    async def fake_page(page):
        return {"data": items, "last_page": 1, "total": len(items)}

    fetched: list[str] = []

    async def fake_one(slug):
        fetched.append(slug)
        return {"text": f"<p>full {slug}</p>"}

    src = HirifySource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", dict)

    out = asyncio.run(src.collect())
    assert len(fetched) == 1                                  # лимит=1 -> одна дозагрузка
    assert fetched == ["s3"]                                  # свежайшая (created_at desc) в приоритете
    enriched = [r for r in out if r.enriched]
    assert len(enriched) == 1 and len(out) == 3               # остальные -> tldr-заглушки
