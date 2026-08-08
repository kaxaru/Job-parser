"""Один бакет «места нет» на все порталы — доменная REMOTE_CITY, а не своя строка у адаптера.

АУДИТ 08.08.2026. Константа `domain/models.py::REMOTE_CITY` заводилась ровно ради этого
(инцидент 07.08.2026: himalayas писал «Worldwide», остальные — «Remote», и один и тот же
случай делился на бакеты 11 978 против 219). Три адаптера в неё всё-таки не сводили:

  * `talanto.py::_clean_city` брал первый сегмент location как есть — «Anywhere in the World»
    (326 записей в кеше), «Worldwide» (8), «World» (5), «Удалённо»/«Удаленно» (3);
  * `getmatch.py::_city` отдавал подпись location_items ДО фолбэка — «Весь мир» (43),
    «Worldwide» (12);
  * `jobicy.py::_city` оставлял «Anywhere» (7) намеренно, «потому что читается».

Итог — четыре строки в срезе 01_cities вместо одной. Подпись пользователю видна только
в карточке, а КЛЮЧОМ она работает в фасете городов ленты и в отчёте, поэтому намеренность
по jobicy устарела: читаемость одной карточки не стоит расщеплённого бакета.

Границу тоже фиксируем: уточнённая география («Remote - United States», «EMEA», «Европа»)
НЕ сводится — там названо ограничение найма, и терять его нельзя.
"""
import pytest

from hrwork.domain.models import REMOTE_CITY
from hrwork.infrastructure.sources import getmatch, jobicy, talanto


def _city_of(source: str, raw: str) -> str:
    """Подпись города, которую адаптер положит в Vacancy для данной локации портала."""
    if source == "getmatch":
        it = {"id": 1, "position": "Python-разработчик", "location_items": [{"label": raw}]}
        return getmatch._normalize(it).vacancy.city
    if source == "talanto":
        it = {"id": "1", "title": "Python-разработчик", "location": raw}
        return talanto._normalize(it).vacancy.city
    it = {"id": 1, "jobTitle": "Python Engineer", "jobGeo": raw}
    return jobicy._normalize(it).vacancy.city


#: локации, означающие «привязки к месту нет» — по одной строке на реальную запись кеша
NO_PLACE = [
    ("getmatch", "Весь мир"),
    ("getmatch", "Worldwide"),
    ("talanto", "Anywhere in the World, 🇦🇩 Andorra, 🇦🇱 Albania"),
    ("talanto", "Worldwide"),
    ("talanto", "World"),
    ("talanto", "Удалённо"),
    ("talanto", "Удаленно"),
    ("talanto", "Remote"),
    ("jobicy", "Anywhere"),
]


def test_remote_city_is_the_literal_bucket_key():
    assert REMOTE_CITY == "Remote"


@pytest.mark.parametrize("source, raw", NO_PLACE)
def test_no_place_label_becomes_the_shared_remote_city(source, raw):
    assert _city_of(source, raw) == "Remote"


def test_all_three_portals_land_in_a_single_bucket():
    """Цель формулируется как одна строка в отчёте: сколько бы ни было исходных подписей,
    бакет один."""
    assert len({_city_of(source, raw) for source, raw in NO_PLACE}) == 1


@pytest.mark.parametrize("source, raw, expected", [
    ("getmatch", "Москва", "Москва"),
    ("getmatch", "Европа", "Европа"),                  # широкая, но НАЗВАННАЯ география
    ("talanto", "Remote - United States", "Remote - United States"),
    ("talanto", "Удалённо по РФ", "Удалённо по РФ"),
    ("talanto", "Москва (м. Киевская)", "Москва (м. Киевская)"),
    ("jobicy", "EMEA", "EMEA"),
    ("jobicy", "Canada, USA", "Canada, USA"),
])
def test_named_geography_is_not_collapsed(source, raw, expected):
    assert _city_of(source, raw) == expected
