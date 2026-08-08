"""Целостность обхода страниц у arbeitnow и themuse: пустая страница != конец выдачи.

Инцидент 07.08.2026 (himalayas): портал под троттлингом начинает отдавать HTTP 200 с пустым
списком, внешне неотличимый от «вакансии кончились». Ретраи транспорта на это не срабатывают
— ответ пришёл, он просто пустой. Обход выходил по первой пустой странице и отчитывался
успехом: 12 043 вакансии из 26 216 легли в кеш под видом полного среза.

himalayas тогда получил перепроверку пустой страницы (`himalayas.py::_get_page`), а два
других адаптера с той же схемой обхода — нет. Здесь проверяется, что урок перенесён:
перепроверка есть у обоих, и обрыв обхода на ДЫРЕ (за пустой страницей в той же пачке
данные ещё есть) не проходит молча.
"""
import asyncio

import pytest

from hrwork.infrastructure.sources import arbeitnow, themuse

#: пустая страница -> 1 запрос + 3 перепроверки (CFG.empty_retries)
PROBES_ON_EMPTY = 4


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


async def _no_sleep(_delay):
    return None


def _bodies(module, monkeypatch, bodies):
    """Подменить сетевой примитив заранее заготовленными телами ответов (последнее — залипает)."""
    urls: list[str] = []

    async def fake_fetch(url, **_kw):
        urls.append(url)
        return bodies[min(len(urls) - 1, len(bodies) - 1)]

    monkeypatch.setattr(module, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(module.asyncio, "sleep", _no_sleep)
    return urls


# ── перепроверка пустой страницы ───────────────────────────────────────────────

def test_arbeitnow_empty_page_is_rechecked_and_throttled_data_is_taken(monkeypatch):
    urls = _bodies(arbeitnow, monkeypatch,
                   [b'{"data": []}', b'{"data": [{"slug": "s1"}]}'])
    got = asyncio.run(arbeitnow.ArbeitnowSource()._get_page(7))
    assert got == [{"slug": "s1"}]
    assert len(urls) == 2                    # пустой ответ перепрошен, данные пришли со 2-й попытки


def test_arbeitnow_page_empty_after_all_rechecks_is_the_end_of_the_list(monkeypatch):
    urls = _bodies(arbeitnow, monkeypatch, [b'{"data": []}'])
    assert asyncio.run(arbeitnow.ArbeitnowSource()._get_page(7)) == []
    assert len(urls) == PROBES_ON_EMPTY


def test_themuse_empty_page_is_rechecked_and_throttled_data_is_taken(monkeypatch):
    urls = _bodies(themuse, monkeypatch,
                   [b'{"results": []}', b'{"results": [{"id": "1"}]}'])
    got = asyncio.run(themuse.ThemuseSource()._get_page("category=IT", 7))
    assert got == [{"id": "1"}]
    assert len(urls) == 2


def test_themuse_page_empty_after_all_rechecks_is_the_end_of_the_list(monkeypatch):
    urls = _bodies(themuse, monkeypatch, [b'{"results": []}'])
    assert asyncio.run(themuse.ThemuseSource()._get_page("category=IT", 7)) == []
    assert len(urls) == PROBES_ON_EMPTY


# ── обрыв обхода на дыре ───────────────────────────────────────────────────────

@pytest.mark.parametrize("chunks, expected", [
    ([[{}], [], [{}]], 1),      # за пустой 6-й страница 7 отдала данные -> это ДЫРА, не конец
    ([[{}], [], []], 0),        # дальше пусто -> честный конец выдачи
    ([[{}], [{}], []], 0),      # пустая последняя -> честный конец выдачи
    ([[], [], []], 0),          # пачка целиком пуста -> конец выдачи
])
def test_arbeitnow_reports_a_break_on_a_hole_only(monkeypatch, chunks, expected):
    fake = _Log()
    monkeypatch.setattr(arbeitnow, "log", fake)
    arbeitnow._warn_if_hole([5, 6, 7], chunks)
    assert len(fake.warnings) == expected


def test_arbeitnow_hole_warning_names_the_empty_page_and_the_page_after_it(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(arbeitnow, "log", fake)
    arbeitnow._warn_if_hole([5, 6, 7], [[{}], [], [{}]])
    assert fake.warnings == [
        "arbeitnow: обход оборван на ПУСТОЙ странице 6, но страница 7 той же пачки отдала "
        "данные — выдача НЕ кончилась, часть вакансий не собрана"]


def test_arbeitnow_reports_the_crawl_hitting_the_page_limit(monkeypatch):
    # Портал отдаёт ~41 страницу, лимит 120 — упереться в него значит, что выдача выросла
    # или сломалась пагинация. Молчаливое усечение здесь неотличимо от успешного сбора.
    fake = _Log()
    monkeypatch.setattr(arbeitnow, "log", fake)
    monkeypatch.setattr(arbeitnow, "CFG", arbeitnow.ArbeitnowCfg(max_pages=3, page_conc=2))
    card = {"slug": "python-backend-engineer-1", "title": "Python Backend Engineer",
            "company_name": "Acme", "location": "Berlin", "remote": True,
            "description": "<p>Python, FastAPI</p>", "created_at": 1786046437}

    async def always_full(page):
        return [{**card, "slug": f"python-backend-engineer-{page}"}]

    src = arbeitnow.ArbeitnowSource()
    monkeypatch.setattr(src, "_get_page", always_full)
    asyncio.run(src.collect())
    assert fake.warnings == [
        "arbeitnow: обход УПЁРСЯ В ЛИМИТ 3 страниц, выдача НЕ исчерпана — часть вакансий "
        "не собрана. Поднять: ARBEITNOW_MAX_PAGES"]


def test_arbeitnow_exhausted_crawl_does_not_warn_about_the_limit(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(arbeitnow, "log", fake)
    monkeypatch.setattr(arbeitnow, "CFG", arbeitnow.ArbeitnowCfg(max_pages=50, page_conc=2))
    card = {"slug": "python-backend-engineer-1", "title": "Python Backend Engineer",
            "company_name": "Acme", "location": "Berlin", "remote": True,
            "description": "<p>Python, FastAPI</p>", "created_at": 1786046437}

    async def one_page(page):
        return [card] if page == 1 else []

    src = arbeitnow.ArbeitnowSource()
    monkeypatch.setattr(src, "_get_page", one_page)
    asyncio.run(src.collect())
    assert fake.warnings == []


@pytest.mark.parametrize("chunks, expected", [
    ([[{}], [], [{}]], 1),
    ([[{}], [], []], 0),
    ([[{}], [{}], []], 0),
    ([[], [], []], 0),
])
def test_themuse_reports_a_break_on_a_hole_only(monkeypatch, chunks, expected):
    fake = _Log()
    monkeypatch.setattr(themuse, "log", fake)
    themuse._warn_if_hole("category=IT", [5, 6, 7], chunks)
    assert len(fake.warnings) == expected


def test_themuse_hole_warning_names_the_query_it_happened_in(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(themuse, "log", fake)
    themuse._warn_if_hole("category=IT", [5, 6, 7], [[{}], [], [{}]])
    assert fake.warnings == [
        "themuse [category=IT]: обход оборван на ПУСТОЙ странице 6, но страница 7 той же "
        "пачки отдала данные — выдача НЕ кончилась, часть вакансий не собрана"]
