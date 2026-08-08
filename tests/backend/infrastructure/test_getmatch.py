"""Тесты источника getmatch: ACL getmatch-API -> VacancyRecord (домен напрямую, без сети)."""
import asyncio

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.role import Role
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure import storage
from hrwork.infrastructure.sources.base import ListIncomplete, check_list_complete
from hrwork.infrastructure.sources.getmatch import (
    CFG,
    GetmatchSource,
    _cached_experience,
    _normalize,
    _sig,
)
from hrwork.infrastructure.storage import files
from hrwork.infrastructure.storage.repository import JsonVacancyRepository


def _check_list_complete(got, *, total, per_page, failed):
    """Сверка глазами getmatch: общая функция `base.check_list_complete` с порогом ПОРТАЛА.
    Порог — часть спецификации адаптера, поэтому берётся из его конфига, а не из литерала."""
    check_list_complete(got, total=total, per_page=per_page, failed=failed,
                        source="getmatch", max_lost_ratio=CFG.list_loss_max_ratio,
                        failed_kind="offset'ы")


ITEM = {
    "id": 35091,
    "position": "Администратор систем сбора событий с конечных точек (EDR)",
    "url": "/vacancies/35091-administrator-sistem-sbora-sobytii",
    "published_at": "2026-08-01T09:00:46.772355",
    "offer_type": "vacancy",
    "company": {"name": "Сбер", "url": "/companies/GNRXrNQz-sber"},
    "location_items": [{"label": "Москва (м. Площадь Ильича)", "format": "office"}],
    "location_requirements": [{"city": "Москва", "country": "Россия", "format": "office"}],
    "skills_objects": [{"name": "PostgreSQL", "slug": "postgresql"},
                       {"name": "Python", "slug": "python"},
                       {"name": "Kafka", "slug": "kafka"}],
    "salary_display_from": 270000, "salary_display_to": 350000,
    "salary_currency": "RUB", "salary_taxes": "gross", "salary_hidden": False,
    "offer_description": "<b>Что делать:</b> развёртывать и настраивать серверы EDR.",
    "description_html": None,
}
FULL = {
    **ITEM,
    "description": "<h2>Задачи</h2>\n<ul><li>развертывание и настройка</li></ul>",
    "seniority": "senior", "seniorities": ["senior"],
    "required_years_of_experience": 3,
}


def test_normalize_maps_to_domain():
    r = _normalize(ITEM, FULL)
    v = r.vacancy
    assert v.id == "getmatch_35091"                     # неймспейс — не столкнётся с id HH
    assert v.source == "getmatch"
    assert v.employer == "Сбер"
    assert v.city == "Москва"                           # из location_requirements, не из label
    assert r.url == "https://getmatch.ru/vacancies/35091-administrator-sistem-sbora-sobytii"
    assert v.schedule.hh_code == "fullDay"
    assert v.experience.hh_id == "between3And6"         # seniority=senior
    assert v.responses is None                          # счётчика откликов API не отдаёт


def test_gross_salary_is_converted_to_net():
    """АУДИТ 08.08.2026: getmatch — единственный источник с gross=True, и он единственный
    не звал `.net()`. На диск запись ложится с `gross: false` (`repository.py::_to_dict`),
    поэтому невычтенный НДФЛ «легализовался» как net и после перезагрузки кеша был
    неотличим от честного. Ожидание — литерал по ставке НДФЛ 13 %: 270 000 -> 234 900,
    350 000 -> 304 500. Прежнее ожидание (270 000, 350 000, gross=True) фиксировало баг."""
    v = _normalize(ITEM, FULL).vacancy
    assert (v.salary.frm, v.salary.to, v.salary.currency) == (234900, 304500, "RUB")
    assert v.salary.gross is False


def test_gross_range_median_matches_the_after_tax_value():
    """Числа инцидента: вилка 200 000–300 000 gross давала медиану 250 000 вместо 217 500
    (завышение 14.9 %) — и она уезжала в медианы отчётов 03/04/06 и в срез по порталам."""
    it = {**ITEM, "salary_display_from": 200000, "salary_display_to": 300000}
    assert _normalize(it, FULL).vacancy.salary.mid == 217500


def test_net_salary_is_not_taxed_twice():
    """salary_taxes=net — вилка уже после налога, второй раз 13 % не вычитаем."""
    it = {**ITEM, "salary_taxes": "net"}
    v = _normalize(it, FULL).vacancy
    assert (v.salary.frm, v.salary.to) == (270000, 350000)


def test_hidden_salary_is_none():
    it = {**ITEM, "salary_display_from": None, "salary_display_to": None,
          "salary_currency": None, "salary_hidden": True}
    assert _normalize(it, FULL).vacancy.salary is None


def test_structured_skills_feed_single_detection_point():
    """skills_objects идут в detect_text, а не в обход _detect_techs."""
    v = _normalize(ITEM, FULL).vacancy
    assert "Python" in v.techs
    assert "PostgreSQL" in v.techs
    # точный член Role, а не флаг `.is_it`: «(EDR)» в тайтле — ключ `Security`
    # таблицы ROLE_PATTERNS (`edr`), класс средств защиты
    assert v.role is Role.SECURITY


def test_full_description_comes_from_card_not_list():
    """В списке description_html всегда null, полный текст — в /api/offers/{id}::description."""
    from_list = _normalize(ITEM)
    from_card = _normalize(ITEM, FULL)
    assert from_list.enriched is False
    assert from_list.description_html == ITEM["offer_description"]
    assert from_card.enriched is True
    assert from_card.description_html == FULL["description"]


def test_grade_absent_without_card():
    """Грейда в списке нет — честный None вместо выдуманного уровня."""
    assert _normalize(ITEM).vacancy.experience is None


# ── Грейд на reuse-пути (описание из кеша, сеть не трогаем) ─────────────────────────────
# РЕГРЕСС 08.08.2026: грейд приходит ТОЛЬКО карточкой, а кеш описаний его не хранил, поэтому
# со второго дня все переиспользованные записи getmatch шли с experience=None — 13 дней из 14
# (до протухания кеша) срез по опыту для портала был пуст.

def test_cached_run_keeps_the_grade_instead_of_dropping_it():
    r = _normalize(ITEM, cached_desc="<p>из кеша</p>", enriched_at="2026-07-31T08:00:00Z",
                   cached_exp=Experience.BETWEEN_3_6)
    assert r.vacancy.experience.hh_id == "between3And6"


@pytest.mark.parametrize("hit, expected", [
    ({"experience_id": "noExperience"}, "noExperience"),
    ({"experience_id": "between1And3"}, "between1And3"),
    ({"experience_id": "between3And6"}, "between3And6"),   # код доменного VO из raw-схемы
    ({"experience_id": "moreThan6"}, "moreThan6"),
    ({"experience_id": ""}, None),                         # пустой код -> грейда нет
    ({"experience_id": None}, None),
    ({"experience_id": "weird"}, None),                    # мусор в кеше -> грейда нет
])
def test_cached_experience_restores_the_grade_from_the_stored_vo_code(hit, expected):
    exp = _cached_experience(hit)
    assert (exp.hh_id if exp else None) == expected


def test_cached_experience_ignores_raw_portal_fields():
    """АУДИТ 08.08.2026 (находка 32): вторая ветка разбора читала сырые `seniority`/`years`,
    но их никто не пишет на диск, поэтому она не срабатывала НИ РАЗУ. Грейд восстанавливает
    `experience_id`, и когда его нет — честный None, а не второй путь к тому же значению."""
    assert _cached_experience({"seniority": "senior", "years": 3}) is None


def test_cached_experience_survives_an_old_entry_without_the_key():
    """Запись кеша СТАРОГО формата (только sig/description_html/requirement/at) не должна
    ронять адаптер — грейд просто остаётся пустым, как было до фикса."""
    old = {"sig": "2026-08-01T09:00:46.772355", "description_html": "<p>x</p>",
           "requirement": "", "at": None}
    assert _cached_experience(old) is None


def test_grade_survives_the_round_trip_through_the_stored_raw_schema(tmp_path, monkeypatch):
    """Замыкание находки 12 целиком, по всему пути: карточка -> VacancyRecord ->
    vacancies_raw.json -> desc-кеш -> снова VO. Сырых полей грейда в схеме хранения для
    этого не нужно: `experience.id` персистится с самого начала."""
    raw = tmp_path / "vacancies_raw.json"
    monkeypatch.setattr(files, "RAW_FILE", raw)
    monkeypatch.setattr(files, "META_FILE", tmp_path / "cache_meta.json")
    JsonVacancyRepository(raw).save([_normalize(ITEM, FULL)])     # день 1: грейд из карточки
    hit = files.load_desc_cache()["getmatch_35091"]               # день 2: карточку не тянем
    exp = _cached_experience(hit)
    assert (exp.hh_id if exp else None) == "between3And6"


def test_sig_tracks_publication_change():
    assert _sig(ITEM) == "2026-08-01T09:00:46.772355"
    assert _sig({**ITEM, "published_at": "2026-08-02T10:00:00"}) != _sig(ITEM)


def test_cached_description_skips_network_path():
    r = _normalize(ITEM, cached_desc="<p>из кеша</p>", enriched_at="2026-07-31T08:00:00Z")
    assert r.description_html == "<p>из кеша</p>"
    assert (r.enriched, r.enriched_at) == (True, "2026-07-31T08:00:00Z")


# ── VO-фабрики getmatch ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seniority,years,expected", [
    ("junior", 1, "between1And3"),
    ("middle", 2, "between1And3"),
    ("senior", 3, "between3And6"),      # грейд важнее лет: senior с years=3 — не junior
    ("lead", 6, "moreThan6"),
    (None, 0, "noExperience"),          # грейда нет -> по годам
    (None, 2, "between1And3"),
    (None, 5, "between3And6"),
    (None, 9, "moreThan6"),
])
def test_experience_from_getmatch(seniority, years, expected):
    assert Experience.from_getmatch(seniority, years).hh_id == expected


def test_experience_unknown_is_none():
    assert Experience.from_getmatch(None, None) is None


@pytest.mark.parametrize("formats,expected", [
    (["remote"], "remote"),
    (["hybrid"], "flexible"),
    (["office"], "fullDay"),
    (["office", "remote"], "remote"),               # приоритет remote > hybrid > офис
    (["office", "hybrid"], "flexible"),
    (["relocation_company"], "fullDay"),            # переезд ради работы В ОФИСЕ
    ([], "fullDay"),
])
def test_schedule_from_getmatch(formats, expected):
    assert Schedule.from_getmatch(formats).hh_code == expected


# ── Город: «места нет» — один бакет на все порталы ──────────────────────────────────────
# АУДИТ 08.08.2026: `_city` отдавал подпись location_items ДО фолбэка на REMOTE_CITY, и
# «Весь мир» (43 записи в кеше) с «Worldwide» (12) становились отдельными строками фасета
# городов рядом с «Remote». Это ровно тот дефект, ради которого REMOTE_CITY заводилась.

@pytest.mark.parametrize("label, expected", [
    ("Весь мир", "Remote"),
    ("Worldwide", "Remote"),
    ("worldwide", "Remote"),                        # регистр портала не фиксирован
    ("Москва (м. Площадь Ильича)", "Москва (м. Площадь Ильича)"),   # настоящий адрес цел
    ("Limassol", "Limassol"),
    ("Европа", "Европа"),                           # широкая, но НАЗВАННАЯ география — не «Remote»
])
def test_no_place_label_collapses_to_the_shared_remote_city(label, expected):
    it = {**ITEM, "location_requirements": [], "location_items": [{"label": label}]}
    assert _normalize(it).vacancy.city == expected


def test_city_without_any_location_is_remote():
    it = {**ITEM, "location_requirements": [], "location_items": []}
    assert _normalize(it).vacancy.city == "Remote"


# ── Полнота списка: сбойная страница не превращается в тихую потерю ─────────────────────
# РЕГРЕСС 08.08.2026: None после ретраев превращался в пустой кусок, обход шёл дальше,
# усечённый срез затирал кеш, а прогон считался успешным (потеря меньше 50 %-порога гейта).

def test_full_list_without_failed_pages_passes():
    _check_list_complete(740, total=740, per_page=100, failed=[])


def test_one_failed_page_of_a_small_portal_only_warns():
    """Порог 25 %: одна страница getmatch — 12.5 % портала в 8 страниц. Ронять из-за неё
    весь прогон нельзя, иначе санити-гейт выбросит сбор ВСЕХ девяти источников."""
    _check_list_complete(700, total=800, per_page=100, failed=[100])


@pytest.mark.parametrize("failed", [
    [100, 200],                                     # 2 страницы из 8 = 25 % — ровно порог
    [100, 200, 300],                                # 37.5 %
])
def test_page_loss_at_or_above_the_threshold_discards_the_run(failed):
    with pytest.raises(ListIncomplete):
        _check_list_complete(800 - len(failed) * 100, total=800, per_page=100, failed=failed)


def _src(monkeypatch, pages, cache=None):
    """Источник с подменённым транспортом: {offset: ответ API} (None = страница не отдалась)."""
    src = GetmatchSource()

    async def fake_page(offset):
        return pages.get(offset)

    async def fake_one(vid):
        return {"description": f"<h2>карточка {vid}</h2>", "seniority": "senior"}

    monkeypatch.setattr(src, "_get_page", fake_page)
    monkeypatch.setattr(src, "_get_one", fake_one)
    monkeypatch.setattr(storage, "load_desc_cache", lambda: dict(cache or {}))
    return src


def test_collect_refuses_a_truncated_list_instead_of_returning_it(monkeypatch):
    pages = {0: {"meta": {"total": 300}, "offers": [ITEM]},
             200: {"offers": [{**ITEM, "id": 3}]}}        # offset=100 не отдался после ретраев
    src = _src(monkeypatch, pages)
    with pytest.raises(ListIncomplete):
        asyncio.run(src.collect())


def test_collect_takes_the_grade_from_cache_without_touching_the_card(monkeypatch):
    """День 2: описание берётся из кеша, карточка не тянется — и грейд обязан выжить."""
    pages = {0: {"meta": {"total": 1}, "offers": [ITEM]}}
    hit = {"sig": ITEM["published_at"], "description_html": "<p>из кеша</p>",
           "requirement": "", "at": None, "experience_id": "between3And6"}
    src = _src(monkeypatch, pages, cache={"getmatch_35091": hit})

    async def boom(vid):
        raise AssertionError("карточка не должна запрашиваться на кеш-пути")

    monkeypatch.setattr(src, "_get_one", boom)
    out = asyncio.run(src.collect())
    assert [(r.vacancy.id, r.vacancy.experience.hh_id) for r in out] == \
           [("getmatch_35091", "between3And6")]


def test_one_broken_card_does_not_kill_the_source(monkeypatch):
    """АУДИТ 08.08.2026: исключение из `_normalize` пробивало до `hh.py::_run_source`, тот
    отдавал [], и санити-гейт замораживал кеш ВСЕХ порталов. Кривая карточка — чужие данные:
    её пропускаем со счётчиком, остальные доезжают."""
    broken = {**ITEM, "id": 999, "company": ["дрейф схемы: список вместо объекта"]}
    pages = {0: {"meta": {"total": 2}, "offers": [ITEM, broken]}}
    src = _src(monkeypatch, pages)
    out = asyncio.run(src.collect())
    assert [r.vacancy.id for r in out] == ["getmatch_35091"]
