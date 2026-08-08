"""Тесты источника talanto: ACL talanto-API -> VacancyRecord (домен напрямую, без сети)."""
import asyncio

import pytest

from hrwork.domain.role import Role
from hrwork.infrastructure import sources, storage
from hrwork.infrastructure.sources import talanto
from hrwork.infrastructure.sources.base import ListIncomplete, check_list_complete
from hrwork.infrastructure.sources.talanto import (
    CFG,
    TalantoCfg,
    TalantoSource,
    _meta_header,
    _normalize,
    _sig,
)


def _check_list_complete(got, *, total, per_page, failed):
    """Сверка глазами talanto: общая функция `base.check_list_complete` с порогом ПОРТАЛА.
    Порог — часть спецификации адаптера, поэтому берётся из его конфига, а не из литерала."""
    check_list_complete(got, total=total, per_page=per_page, failed=failed,
                        source="talanto", max_lost_ratio=CFG.list_loss_max_ratio,
                        failed_kind="offset'ы")

ITEM = {
    "id": "8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875",
    "title": "Программист Python", "company": "А7-агент (ПСБ)",
    "location": "Москва (м. Киевская)", "remote_type": "office",
    "level": "mid", "employment_type": "full",
    "salary_min": 300000, "salary_max": 400000, "salary_currency": "RUB",
    "published_at": "2026-07-22T18:13:56Z",
    "skills": ["crm", "Python", "API", "Базы данных"],
    "last_verified_at": "2026-07-22T18:13:57.360467Z",
}


def test_normalize_maps_to_domain():
    r = _normalize(ITEM)
    v = r.vacancy
    assert v.id == "talanto_8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875"   # неймспейс
    assert v.source == "talanto"
    assert r.url == "https://talanto.work/jobs/8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875"
    assert v.employer == "А7-агент (ПСБ)"
    assert v.city == "Москва (м. Киевская)"
    assert v.schedule.hh_code == "fullDay"                 # office -> OFFICE
    assert (v.salary.frm, v.salary.to, v.salary.currency) == (300000, 400000, "RUB")
    assert v.experience.hh_id == "between1And3"            # mid
    # skills подмешаны в detect_text/requirement -> детект стека сработал в адаптере
    assert "Python" in v.techs
    # точный член Role, а не флаг `.is_it` («любая из 16 IT-ролей»): тайтл «Программист
    # Python» ловится ключом `Разработчик` таблицы ROLE_PATTERNS
    assert v.role is Role.DEVELOPER
    assert r.sig == ITEM["last_verified_at"]


# Расписание маппит ДОМЕН (Schedule.from_talanto), у адаптера своей таблицы больше нет.
# АУДИТ 08.08.2026: копия `_SCHED` в talanto — тот же класс дефекта, что был с грейдами:
# адаптерная таблица тихо расходится с доменной. Различие видно на входах, которые понимает
# доменный парсер и не понимала копия: он нормализует ВНЕШНИЕ ПРОБЕЛЫ, а `_SCHED.get(raw.lower())`
# на « Remote » промахивался и молча отдавал офис.
@pytest.mark.parametrize("remote_type, hh_code", [
    ("remote", "remote"), ("hybrid", "flexible"), ("office", "fullDay"),
    ("Remote", "remote"), ("REMOTE", "remote"),            # регистр портала не фиксирован
    (" remote ", "remote"), ("\tHybrid\n", "flexible"),    # внешние пробелы — работа парсера
    (None, "fullDay"), ("", "fullDay"), ("weird", "fullDay"),   # неизвестное -> дефолт OFFICE
])
def test_normalize_schedule_mapping(remote_type, hh_code):
    it = {**ITEM, "remote_type": remote_type}
    assert _normalize(it).vacancy.schedule.hh_code == hh_code


# Грейд маппит ДОМЕН (Experience.from_grades), у адаптера своей таблицы больше нет.
# АУДИТ 07.08.2026: копия в talanto расходилась с доменом — «junior» она клала в
# «Без опыта», тогда как на hirify/getmatch/himalayas/jobicy/themuse тот же junior давал
# «1–3 года». Прежнее ожидание ("junior", "noExperience") фиксировало этот баг как
# требование. Правильное значение берётся из домена, а не из бывшего поведения адаптера.
@pytest.mark.parametrize("level, exp", [
    ("junior", "between1And3"),                            # было noExperience — расхождение
    ("intern", "noExperience"), ("trainee", "noExperience"),
    ("mid", "between1And3"), ("middle", "between1And3"),
    ("senior", "between3And6"), ("lead", "moreThan6"),
    ("principal", "moreThan6"), ("head", "moreThan6"),
    ("Mid-level", "between1And3"),                         # точное равенство это не ловило
    ("Senior/Lead", "between3And6"),                       # вилка -> младший из двух
    (None, None), ("weird", None),                         # мягкий контракт: неизвестное -> None
])
def test_normalize_level_mapping(level, exp):
    v = _normalize({**ITEM, "level": level}).vacancy
    assert (v.experience.hh_id if v.experience else None) == exp


def test_normalize_no_salary_is_none():
    it = {**ITEM, "salary_min": None, "salary_max": None}
    assert _normalize(it).vacancy.salary is None


# ── Период вилки talanto ────────────────────────────────────────────────────────────────
# Вилка входит в домен через `Salary.monthly` с ЯВНЫМ периодом MONTH. Инференс по величине
# (как у hirify/web3) здесь НЕ применяется: замер по кешу 08.08.2026 показал, что talanto
# делит годовые на 12 на своей стороне (35.9 % USD-границ имеют «/12-подпись» — как у уже
# приведённого к месяцу hirify с 52.7 %, при 0.0 % у hh). См. комментарий в talanto._normalize.

@pytest.mark.parametrize("minimum, maximum, currency, expected", [
    (300000, 400000, "RUB", (300000, 400000)),   # рублёвая месячная — НЕ делится на 12
    (110000, None, "RUR", (110000, None)),       # выше порога инференса $25 000, но это рубли
    (None, 12000, "USD", (None, 12000)),
    (200, 300, "USD", (200, 300)),               # ниже порога «часовой» — НЕ умножается на 160
])
def test_range_is_taken_as_monthly_without_guessing(minimum, maximum, currency, expected):
    v = _normalize({**ITEM, "salary_min": minimum, "salary_max": maximum,
                    "salary_currency": currency}).vacancy
    assert (v.salary.frm, v.salary.to) == expected


def test_numeric_string_from_api_becomes_a_number():
    """Доменная фабрика приводит границу к числу; раньше строка уезжала в вилку как есть
    и ломала арифметику медиан."""
    v = _normalize({**ITEM, "salary_min": "300000", "salary_max": None}).vacancy
    assert v.salary.frm == 300000


def test_non_numeric_range_is_dropped():
    """«по договорённости» вместо числа — вилки нет, а не вилка из мусора."""
    it = {**ITEM, "salary_min": "по договорённости", "salary_max": None}
    assert _normalize(it).vacancy.salary is None


def test_normalize_enrich_marks_and_meta():
    full = {"description": "<p>Обязанности…</p>", "url": "https://telegram.me/jobs1c/36305"}
    r = _normalize(ITEM, full)
    assert r.enriched is True
    assert r.enriched_at is not None
    assert "Обязанности" in r.description_html
    assert "telegram.me/jobs1c" in r.description_html      # первоисточник в мета-шапке
    r2 = _normalize(ITEM)                                  # без карточки — не enriched
    assert r2.enriched is False
    assert r2.enriched_at is None


def test_normalize_cached_desc_skips_network_flags():
    r = _normalize(ITEM, cached_desc="<p>из кеша</p>", enriched_at="2026-07-20T00:00:00")
    assert r.enriched is True
    assert r.description_html == "<p>из кеша</p>"
    assert r.enriched_at == "2026-07-20T00:00:00"          # метка реального фетча переносится


def test_sig_falls_back_to_published():
    assert _sig({"published_at": "2026-07-01"}) == "2026-07-01"
    assert _sig({}) == ""


def test_meta_header_escapes_html():
    """АУДИТ 09.08.2026: проверялось только ОТСУТСТВИЕ подстроки — `return ""`, шапка без
    грейда и вырезание тегов вместо экранирования (`re.sub(r'<[^>]+>', '', …)`) проходили
    одинаково зелёными. Сверяем строку целиком, в формате BARE_HEADER."""
    assert _meta_header({"level": "<b>x</b>"}, None) == (
        "<p>🎯 &lt;b&gt;x&lt;/b&gt; · 📌 talanto.work</p>")


@pytest.mark.parametrize("loc, expected", [
    # АУДИТ 08.08.2026: «Anywhere in the World» (326 записей в кеше), «Worldwide» (8),
    # «Удалённо»/«Удаленно» (3) держали свои строки в фасете городов рядом с «Remote»
    # (9 286). Подпись «места нет» одна на все порталы — доменная REMOTE_CITY.
    # Прежнее ожидание ("Anywhere in the World") фиксировало этот дефект как требование.
    ("Anywhere in the World, 🇦🇩 Andorra, 🇦🇱 Albania", "Remote"),
    ("Worldwide", "Remote"),
    ("World", "Remote"),
    ("Удалённо", "Remote"),                             # ё нормализуется парсером
    ("Удаленно", "Remote"),
    ("Remote work", "Remote"),
    ("Remote, Afghanistan, Albania, Algeria", "Remote"),
    ("Москва (м. Киевская)", "Москва (м. Киевская)"),   # настоящий город цел
    # География НАЗВАНА — не сводим, иначе потеряем ограничение найма
    ("Remote - United States", "Remote - United States"),
    ("Удалённо по РФ", "Удалённо по РФ"),
    (None, ""),
])
def test_clean_city_collapses_country_lists(loc, expected):
    from hrwork.infrastructure.sources.talanto import _clean_city
    assert _clean_city(loc) == expected


def test_clean_city_caps_length():
    from hrwork.infrastructure.sources.talanto import _clean_city
    assert len(_clean_city("X" * 500)) == 80        # мегастрока без запятых — режется по длине


def test_normalize_city_cleaned():
    r = _normalize({**ITEM, "location": "Remote, Argentina, Bolivia, Brazil, Chile"})
    assert r.vacancy.city == "Remote"


@pytest.mark.parametrize("key", ["hh", "hirify", "talanto", "getmatch"])
def test_registry_returns_the_source_registered_under_that_name(key):
    # `is not None` проходило и на объекте ЧУЖОГО источника — то есть на перепутанной
    # строке в `@register_source`, ради которой реестр и проверяют (аудит 09.08.2026)
    assert sources.get_source(key).name == key


def test_unregistered_name_has_no_source():
    assert sources.get_source("unknown") is None


# ── Полнота списка: сбойная страница не превращается в тихую потерю ─────────────────────
# РЕГРЕСС 08.08.2026: None после ретраев превращался в пустой кусок, обход шёл дальше, итог
# логировался без сверки с total. Транзиентный сбой на offset=12000 стоил ~100 записей,
# сбор считался успешным (потеря меньше 50 %-порога санити-гейта), и усечённый срез затирал кеш.

def test_full_list_without_failed_pages_passes():
    _check_list_complete(40000, total=40000, per_page=100, failed=[])


@pytest.mark.parametrize("pages_lost", [1, 4, 7])
def test_loss_below_two_percent_only_warns(pages_lost):
    """Порог 2 % (как у hirify — портал того же размера и формы): одна страница talanto —
    100 записей из 40 000, 0.25 %. Пропущенное вернётся следующим сбором."""
    _check_list_complete(40000 - pages_lost * 100, total=40000, per_page=100,
                         failed=[i * 100 for i in range(1, pages_lost + 1)])


@pytest.mark.parametrize("pages_lost", [8, 20])
def test_loss_at_or_above_two_percent_discards_the_run(pages_lost):
    """8 страниц = 800 записей = 2 %: восстановление описаний уже не влезает в дневной
    TALANTO_ENRICH_MAX=600, и усечённый срез дороже пропуска прогона."""
    with pytest.raises(ListIncomplete):
        _check_list_complete(40000 - pages_lost * 100, total=40000, per_page=100,
                             failed=[i * 100 for i in range(1, pages_lost + 1)])


def _src(monkeypatch, pages, cache=None):
    """Источник с подменённым транспортом: {offset: ответ API} (None = страница не отдалась)."""
    src = TalantoSource()

    async def fake_page(offset):
        return pages.get(offset)

    async def fake_one(vid):
        return {"description": f"<p>карточка {vid}</p>", "url": "https://t.me/jobs/1"}

    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", lambda: dict(cache or {}))
    return src


def test_collect_refuses_a_truncated_list_instead_of_returning_it(monkeypatch):
    # 3 страницы по 100 при total=300, вторая не отдалась после ретраев -> 33 % потери
    pages = {0: {"items": [ITEM], "total": 300},
             200: {"items": [{**ITEM, "id": "c"}]}}
    src = _src(monkeypatch, pages)
    with pytest.raises(ListIncomplete):
        asyncio.run(src.collect())


def test_one_broken_card_does_not_kill_the_source(monkeypatch):
    """АУДИТ 08.08.2026: исключение из `_normalize` пробивало до `hh.py::_run_source`, тот
    отдавал [], и санити-гейт замораживал кеш ВСЕХ порталов."""
    broken = {**ITEM, "id": "broken", "title": {"ru": "дрейф схемы: объект вместо строки"}}
    pages = {0: {"items": [ITEM, broken], "total": 2}}
    src = _src(monkeypatch, pages)
    out = asyncio.run(src.collect())
    assert [r.vacancy.id for r in out] == ["talanto_8bbcfc26-f6d7-47de-b6ee-ccc89d4dd875"]


# ── Протухший кеш вместо пустого описания, когда бюджет enrich исчерпан ─────────────────
# РЕГРЕСС 08.08.2026 (находка 13): протухшая по времени запись уходила в todo, не влезала
# в бюджет и перезаписывалась ОДНОЙ мета-шапкой — уже скачанное описание ТЕРЯЛОСЬ.
# У talanto это дороже, чем у hirify: ~40k вакансий при TALANTO_ENRICH_MAX=600 и жизни
# записи 14 дней дают потолок покрытия 8 400 = 21 %, то есть экспирация съедает бюджет
# целиком и никогда-не-обогащённый хвост не доходит до /jobs/{id} никогда.

#: мета-шапка карточки без первоисточника: только грейд и портал. Эмодзи записаны escape'ами
#: намеренно: консоль проекта в cp1251, и упавший тест иначе печатается ошибкой кодека.
BARE_HEADER = "<p>\U0001f3af mid · \U0001f4cc talanto.work</p>"

SIG = "2026-07-01T00:00:00Z"


def _card(vid, title="Python-разработчик", published="2026-07-01T00:00:00Z", sig=SIG):
    return {"id": vid, "title": title, "level": "mid",
            "published_at": published, "last_verified_at": sig, "skills": []}


def _stale_hit(desc, at):
    return {"sig": SIG, "description_html": desc, "requirement": "", "at": at}


def _old_at():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(tz=timezone.utc) - timedelta(days=20)).isoformat()


def _collect(monkeypatch, items, cache, enrich_max):
    """Прогон collect с подменённым транспортом; отдаёт (записи по id, список id за карточкой)."""
    monkeypatch.setattr(talanto, "CFG", TalantoCfg(enrich_max=enrich_max))
    fetched: list[str] = []

    async def fake_page(offset):
        return {"items": items, "total": len(items)} if offset == 0 else None

    async def fake_one(vid):
        fetched.append(vid)
        return {"description": f"<p>fresh {vid}</p>"}

    src = TalantoSource()
    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", lambda: dict(cache))
    out = asyncio.run(src.collect())
    return {r.vacancy.id: r for r in out}, fetched


def test_stale_description_survives_when_the_enrich_budget_is_spent(monkeypatch):
    old = _old_at()
    by, fetched = _collect(
        monkeypatch,
        items=[_card("a"), _card("b", published="2026-07-02T00:00:00Z")],
        cache={"talanto_a": _stale_hit("<p>old A</p>", old),
               "talanto_b": _stale_hit("<p>old B</p>", old)},
        enrich_max=1)

    assert fetched == ["b"]                                   # бюджет 1 -> свежайшей
    assert by["talanto_b"].description_html == BARE_HEADER + "<p>fresh b</p>"
    assert by["talanto_a"].description_html == "<p>old A</p>"   # НЕ одна мета-шапка
    assert by["talanto_a"].enriched is True
    assert by["talanto_a"].enriched_at == old                 # метка не обнуляется: обновим позже


def test_never_enriched_card_gets_the_budget_before_the_stale_one(monkeypatch):
    # У протухшей описание переживает прогон (fallback выше), у никогда-не-обогащённой
    # альтернатива — пустая карточка. Порядок бюджета решает, кто из них останется без текста.
    by, fetched = _collect(
        monkeypatch,
        items=[_card("stale", published="2026-07-09T00:00:00Z"),      # СВЕЖЕЕ
               _card("new", published="2026-07-01T00:00:00Z")],       # старее
        cache={"talanto_stale": _stale_hit("<p>old stale</p>", _old_at())},
        enrich_max=1)

    assert fetched == ["new"]
    assert by["talanto_new"].description_html == BARE_HEADER + "<p>fresh new</p>"
    assert by["talanto_stale"].description_html == "<p>old stale</p>"


def test_changed_vacancy_never_reuses_the_description_of_the_old_version(monkeypatch):
    # sig другой -> вакансию переписали. Старое описание относится к другой версии и как
    # fallback не годится: показать его было бы враньём, пустая карточка честнее.
    by, fetched = _collect(
        monkeypatch,
        items=[_card("a", sig="2026-07-30T00:00:00Z")],
        cache={"talanto_a": _stale_hit("<p>old A</p>", _old_at())},
        enrich_max=0)

    assert fetched == []
    assert by["talanto_a"].description_html == BARE_HEADER
    assert by["talanto_a"].enriched is False


def test_non_it_vacancy_keeps_its_cached_description_instead_of_vanishing(monkeypatch):
    """АУДИТ 08.08.2026: не-IT тайтл мимо enrich шёл правильно, но запись, уже лежащая
    в кеше с ПРОТУХШИМ описанием, не попадала ни в reuse, ни в todo, ни в хвост — и молча
    исчезала из среза целиком. Тайтл мог стать не-IT после правки словаря отсева."""
    by, fetched = _collect(
        monkeypatch,
        items=[_card("courier", title="Курьер на личном авто")],
        cache={"talanto_courier": _stale_hit("<p>old courier</p>", _old_at())},
        enrich_max=600)

    assert fetched == []                                      # карточку не-IT не тянем
    assert by["talanto_courier"].description_html == "<p>old courier</p>"


def test_non_it_vacancy_without_a_cached_description_still_reaches_the_slice(monkeypatch):
    by, fetched = _collect(
        monkeypatch,
        items=[_card("courier", title="Курьер на личном авто")],
        cache={},
        enrich_max=600)

    assert fetched == []
    assert by["talanto_courier"].description_html == BARE_HEADER
