"""Тесты источника hirify: ACL hirify-API -> VacancyRecord (домен напрямую, без сети)."""
import asyncio

import pytest

from hrwork.domain.role import Role
from hrwork.infrastructure import sources, storage
from hrwork.infrastructure.sources import base, hirify
from hrwork.infrastructure.sources.base import ListIncomplete, check_list_complete
from hrwork.infrastructure.sources.hirify import (
    CFG,
    HirifyCfg,
    HirifySource,
    _clean_company,
    _meta_header,
    _normalize,
)


def _check_list_complete(got, *, total, per_page, failed):
    """Сверка глазами hirify: общая функция `base.check_list_complete` с порогом ПОРТАЛА.
    Порог — часть спецификации адаптера, поэтому берётся из его конфига, а не из литерала."""
    check_list_complete(got, total=total, per_page=per_page, failed=failed,
                        source="hirify", max_lost_ratio=CFG.list_loss_max_ratio)


class _Log:
    """Перехват строк лога: у loguru формат — str.format с позиционными аргументами."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg.format(*args))

    def debug(self, msg, *args):
        pass

    def info(self, msg, *args):
        pass

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
    assert "python" in r.requirement.lower()
    assert "Backend" in r.requirement
    assert "Python" in v.techs
    # точный член Role, а не флаг `.is_it`: тайтл «Python Developer (Middle/Junior)»
    # ловится ключом `Разработчик` (`developer`) таблицы ROLE_PATTERNS
    assert v.role is Role.DEVELOPER


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
    # имя зарегистрированного источника, а не `is not None`: перепутанная строка
    # в `@register_source` вернула бы чужой адаптер и тест бы не заметил
    assert sources.get_source("hh").name == "hh"
    assert sources.get_source("hirify").name == "hirify"
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
    # АУДИТ 09.08.2026: пять проверок по подстрокам — «650» прошло бы и приклеившись
    # к сумме вилки, а пропажа подписи «(норм.)» осталась бы незамеченной. Рядом, в
    # test_meta_header_reads_usd_…, шапка уже сверяется точно; сверяем так же.
    head, _, body = d.partition("</p>")
    assert _header_parts(head + "</p>") == [
        "United Kingdom",             # страна-наниматель
        "English B2",                 # минимальный уровень английского
        "~$650 USD (норм.)",          # нормализованный USD (мета)
        "hirify.me"]
    assert body == "<p>Big job description</p>"   # полный текст из /vacancies/{slug}


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
    assert (v.salary.frm, v.salary.to) == (5000, 7000)    # годовая /12 -> месяц


def test_normalize_sig_and_enriched_flag():
    # tldr-заглушка -> enriched False, sig из created_at (updated_at нет)
    r = _normalize(ITEM)
    assert r.enriched is False
    assert r.sig == ITEM["created_at"]
    # updated_at приоритетнее created_at
    assert _normalize({**ITEM, "updated_at": "2026-07-07T10:00:00Z"}).sig == "2026-07-07T10:00:00Z"
    # полный текст -> enriched True
    r3 = _normalize(ITEM, {"text": "<p>full</p>"})
    assert r3.enriched is True
    assert "full" in r3.description_html
    # описание из кеша -> enriched True, html как есть, сеть не нужна
    r4 = _normalize(ITEM, cached_desc="<p>cached</p>")
    assert r4.enriched is True
    assert r4.description_html == "<p>cached</p>"


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


# ── Мета-шапка: гейт по типу, а не по truthy ───────────────────────────────────

def _header_parts(header: str) -> list[str]:
    """Сегменты мета-шапки без эмодзи-префиксов (консоль проекта — cp1251)."""
    body = header.removeprefix("<p>").removesuffix("</p>")
    return [p.split(" ", 1)[1] if " " in p else p for p in body.split(" · ")]


@pytest.mark.parametrize("salary_in_usd, expected", [
    (120000, ["Remote", "~$120,000 USD (норм.)", "hirify.me"]),
    # БАГ 08.08.2026: строковое значение из выдачи уходило в спецификатор `:,` и давало
    # ValueError («Cannot specify ',' with 's'») — падала нормализация ВСЕЙ страницы.
    ("120000", ["Remote", "~$120,000 USD (норм.)", "hirify.me"]),
    (120000.4, ["Remote", "~$120,000 USD (норм.)", "hirify.me"]),
    (None, ["Remote", "hirify.me"]),
    ("мусор", ["Remote", "hirify.me"]),
    (0, ["Remote", "hirify.me"]),               # 0 у портала значит «нет данных»
])
def test_meta_header_reads_usd_as_a_number_whatever_the_portal_sent(salary_in_usd, expected):
    header = _meta_header({"salary": {"salary_in_usd": salary_in_usd, "currency": "USD"}})
    assert _header_parts(header) == expected


def test_broken_card_is_skipped_without_killing_the_source(monkeypatch):
    # РЕГРЕСС 08.08.2026: исключение из _normalize пробивало до hh.py::_run_source, источник
    # отдавал [], и санити-гейт замораживал кеш ВСЕХ порталов. Кривая карточка — чужие данные:
    # пропускается поштучно. Здесь ломается regions (список строк вместо объектов).
    items = [
        {"id": 1, "slug": "a", "title": "A", "grades": [], "work_format": [],
         "created_at": "2026-07-01T00:00:00Z"},
        {"id": 2, "slug": "b", "title": "B", "grades": [], "work_format": [],
         "created_at": "2026-07-02T00:00:00Z", "regions": ["United Kingdom"]},
        {"id": 3, "slug": "c", "title": "C", "grades": [], "work_format": [],
         "created_at": "2026-07-03T00:00:00Z"},
    ]

    async def fake_page(page):
        return {"data": items, "last_page": 1, "total": 3, "per_page": 3}

    async def fake_one(slug):
        return {"text": f"<p>full {slug}</p>"}

    src = HirifySource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", dict)

    out = asyncio.run(src.collect())
    assert sorted(r.vacancy.id for r in out) == ["hirify_1", "hirify_3"]


# ── Сверка списка с total: сбойная страница != пустая страница ─────────────────

def test_list_without_failed_pages_passes_silently(monkeypatch):
    # Недобор без сбойных страниц — норма: выдача сдвигается между запросами
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    _check_list_complete(17990, total=18000, per_page=100, failed=[])
    assert fake.warnings == []


@pytest.mark.parametrize("failed_pages", [1, 2, 3])
def test_page_loss_below_two_percent_is_reported_but_the_run_survives(monkeypatch, failed_pages):
    # Порог 2 %: одна страница — 100 записей из 18 000 (0.55 %). Ронять весь прогон из-за
    # одного транзиентного сбоя нельзя — записи вернутся следующим сбором.
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    pages = list(range(1, failed_pages + 1))
    _check_list_complete(18000 - failed_pages * 100, total=18000, per_page=100, failed=pages)
    assert len(fake.warnings) == 1


def test_page_loss_warning_names_the_lost_pages(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    _check_list_complete(17900, total=18000, per_page=100, failed=[41])
    assert fake.warnings == [
        "hirify: страниц не отдалось 1 (~100 записей, 0.6% от 18000) — срез неполный, "
        "страницы: 41"]


@pytest.mark.parametrize("failed_pages", [4, 10, 40])
def test_page_loss_above_two_percent_discards_the_run(failed_pages):
    # 4 страницы = 400 записей = 2.2 % — усечённый срез дороже пропуска прогона: он затирает
    # кеш, а восстановление описаний не уложится в дневной HIRIFY_ENRICH_MAX=600.
    with pytest.raises(ListIncomplete):
        _check_list_complete(18000 - failed_pages * 100, total=18000, per_page=100,
                             failed=list(range(1, failed_pages + 1)))


def test_collect_refuses_a_truncated_list_instead_of_returning_it(monkeypatch):
    # РЕГРЕСС 08.08.2026: страница, не отдавшаяся после всех ретраев, превращалась в пустой
    # кусок, обход шёл дальше, и усечённый срез затирал кеш — потеря меньше 50 %-порога
    # санити-гейта, поэтому сбор считался успешным.
    page_data = {
        1: {"data": [{"id": 1, "slug": "a", "title": "A", "grades": [], "work_format": []}],
            "last_page": 3, "total": 3, "per_page": 1},
        3: {"data": [{"id": 3, "slug": "c", "title": "C", "grades": [], "work_format": []}]},
    }

    async def fake_page(page):
        return page_data.get(page)              # страница 2 не отдалась после ретраев

    async def fake_one(slug):
        return {"text": f"<p>full {slug}</p>"}

    src = HirifySource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", dict)

    with pytest.raises(ListIncomplete):
        asyncio.run(src.collect())


# ── Протухший кеш вместо tldr, когда бюджет enrich исчерпан ────────────────────

#: мета-шапка карточки без регионов/английского/вилки. Эмодзи записаны escape'ами
#: намеренно: консоль проекта в cp1251, и упавший тест иначе печатается ошибкой кодека.
BARE_HEADER = "<p>\U0001f30d Remote · \U0001f4cc hirify.me</p>"


def _stale_hit(desc, at):
    return {"sig": "2026-07-01T00:00:00Z", "description_html": desc, "requirement": "", "at": at}


def test_stale_description_survives_when_the_enrich_budget_is_spent(monkeypatch):
    # РЕГРЕСС 08.08.2026: протухшая по времени запись уходила в todo, не влезала в бюджет и
    # перезаписывалась шапкой+tldr — уже скачанное описание ТЕРЯЛОСЬ. Протухшее лучше tldr.
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()
    items = [
        {"id": 1, "slug": "a", "title": "A", "updated_at": "2026-07-01T00:00:00Z",
         "created_at": "2026-07-01T00:00:00Z", "grades": [], "work_format": [], "tldr": "tldr A"},
        {"id": 2, "slug": "b", "title": "B", "updated_at": "2026-07-01T00:00:00Z",
         "created_at": "2026-07-02T00:00:00Z", "grades": [], "work_format": [], "tldr": "tldr B"},
    ]
    monkeypatch.setattr(hirify, "CFG", HirifyCfg(enrich_max=1))

    async def fake_page(page):
        return {"data": items, "last_page": 1, "total": 2, "per_page": 2}

    fetched = []

    async def fake_one(slug):
        fetched.append(slug)
        return {"text": f"<p>fresh {slug}</p>"}

    src = HirifySource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", lambda: {
        "hirify_1": _stale_hit("<p>old A</p>", old),
        "hirify_2": _stale_hit("<p>old B</p>", old)})

    out = asyncio.run(src.collect())
    by = {r.vacancy.id: r for r in out}
    assert fetched == ["b"]                                   # бюджет 1 -> свежайшей
    assert by["hirify_2"].description_html == BARE_HEADER + "<p>fresh b</p>"
    assert by["hirify_1"].description_html == "<p>old A</p>"  # НЕ шапка+tldr
    assert by["hirify_1"].enriched is True
    assert by["hirify_1"].enriched_at == old                  # метка не обнуляется: обновим позже


def test_never_enriched_card_gets_the_budget_before_the_stale_one(monkeypatch):
    # Арифметика: 18k вакансий при бюджете 600/день и жизни записи 14 дней. Если бюджет
    # съедает экспирация, никогда-не-обогащённый хвост не доходит до enrich НИКОГДА.
    # У протухшей описание переживает прогон, у новой альтернатива — tldr.
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()
    items = [
        {"id": 1, "slug": "stale", "title": "A", "updated_at": "2026-07-01T00:00:00Z",
         "created_at": "2026-07-09T00:00:00Z", "grades": [], "work_format": []},   # СВЕЖЕЕ
        {"id": 2, "slug": "new", "title": "B", "updated_at": "2026-07-02T00:00:00Z",
         "created_at": "2026-07-01T00:00:00Z", "grades": [], "work_format": []},   # старее
    ]
    monkeypatch.setattr(hirify, "CFG", HirifyCfg(enrich_max=1))

    async def fake_page(page):
        return {"data": items, "last_page": 1, "total": 2, "per_page": 2}

    fetched = []

    async def fake_one(slug):
        fetched.append(slug)
        return {"text": f"<p>fresh {slug}</p>"}

    src = HirifySource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", lambda: {
        "hirify_1": _stale_hit("<p>old A</p>", old)})

    out = asyncio.run(src.collect())
    by = {r.vacancy.id: r for r in out}
    assert fetched == ["new"]
    assert by["hirify_2"].description_html == BARE_HEADER + "<p>fresh new</p>"
    assert by["hirify_1"].description_html == "<p>old A</p>"


def test_changed_vacancy_never_reuses_the_description_of_the_old_version(monkeypatch):
    # sig другой -> вакансию переписали. Старое описание относится к другой версии и как
    # fallback не годится: показать его было бы враньём, tldr честнее.
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()
    items = [{"id": 1, "slug": "a", "title": "A", "updated_at": "2026-07-30T00:00:00Z",
              "created_at": "2026-07-01T00:00:00Z", "grades": [], "work_format": [],
              "tldr": "tldr A"}]
    monkeypatch.setattr(hirify, "CFG", HirifyCfg(enrich_max=0))

    async def fake_page(page):
        return {"data": items, "last_page": 1, "total": 1, "per_page": 1}

    src = HirifySource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(storage, "load_desc_cache", lambda: {
        "hirify_1": _stale_hit("<p>old A</p>", old)})     # sig записи — 2026-07-01, вакансии — 07-30

    out = asyncio.run(src.collect())
    assert out[0].enriched is False
    assert "old A" not in out[0].description_html
    assert "tldr A" in out[0].description_html


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
    assert len(enriched) == 1
    assert len(out) == 3                                      # остальные -> tldr-заглушки
