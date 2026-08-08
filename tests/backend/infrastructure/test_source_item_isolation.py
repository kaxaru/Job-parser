"""Кривая карточка не роняет источник целиком — jobicy и web3.career.

АУДИТ 08.08.2026. Без изоляции НА ЭЛЕМЕНТЕ исключение из `_normalize` пробивает до
`hh.py::_run_source`, тот отдаёт `[]` по источнику, а санити-гейт (`hh.py::_degraded_source`)
видит стопроцентную просадку и НЕ перезаписывает кеш — то есть одна карточка чужого портала
замораживает данные ВСЕХ девяти. Правильное поведение описано в `base.py::normalize_each`:
сбойную карточку пропускаем со счётчиком и warning, остальные доезжают.

«Кривизна» здесь — не выдуманная: это дрейф внешней схемы (скаляр там, где был список),
ровно тот класс, что дал исходный инцидент в `hirify.py::_meta_header` (`:,`-формат строки).
"""
import asyncio

from hrwork.infrastructure.sources import jobicy, web3career


def _jobicy_card(**kw):
    base = {
        "id": 1,
        "jobTitle": "Python Backend Engineer",
        "companyName": "Acme",
        "jobGeo": "USA",
        "jobLevel": "Mid-Level",
        "jobIndustry": ["Engineering"],
        "jobExcerpt": "Build APIs",
        "jobDescription": "<p>Python, FastAPI, PostgreSQL</p>",
        "pubDate": "2026-08-06T16:30:02+00:00",
        "url": "https://jobicy.com/jobs/1",
    }
    return {**base, **kw}


def _web3_card(**kw):
    base = {
        "id": 1,
        "title": "Python Data Engineer",
        "company": "Crypto.com",
        "city": "Singapore",
        "country": "Singapore",
        "location": "Singapore",
        "is_remote": True,
        "tags": ["python", "backend"],
        "description": "<p>Build data pipelines with Python and Airflow.</p>",
        "apply_url": "https://web3.career/r/1",
        "date_epoch": 1784129943,
        "salary_min_value": None,
        "salary_max_value": None,
        "salary_currency": None,
        "salary_unit": None,
    }
    return {**base, **kw}


def test_jobicy_broken_card_is_skipped_and_the_rest_survive(monkeypatch):
    broken = _jobicy_card(id=2, jobIndustry=42)      # скаляр вместо списка -> TypeError
    src = jobicy.JobicySource()

    async def fake_get(industry):
        return [_jobicy_card(), broken]

    monkeypatch.setattr(jobicy, "JOBICY_INDUSTRIES", [""])
    monkeypatch.setattr(src, "_get", fake_get)
    out = asyncio.run(src.collect())
    assert [r.vacancy.id for r in out] == ["jobicy_1"]


def test_web3_broken_card_is_skipped_and_the_rest_survive(monkeypatch):
    broken = _web3_card(id=2, tags=7)                # скаляр вместо списка -> TypeError
    src = web3career.Web3CareerSource()

    async def fake_tag(tag):
        return [_web3_card(), broken]

    monkeypatch.setattr(web3career, "WEB3_TOKEN", "test-token")
    monkeypatch.setattr(web3career, "WEB3_TAGS", ["python"])
    monkeypatch.setattr(src, "_get_tag", fake_tag)
    out = asyncio.run(src.collect())
    assert [r.vacancy.id for r in out] == ["web3_1"]
