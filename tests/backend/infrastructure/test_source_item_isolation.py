"""Кривая карточка не роняет источник целиком — тест ОБЩЕЙ механики (`base.normalize_each`).

АУДИТ 08.08.2026 + 22.09.2026 (§6). Без изоляции НА ЭЛЕМЕНТЕ исключение из `_normalize`
пробивает до `hh.py::_run_source`, тот отдаёт `[]` по источнику, а санити-гейт
(`hh.py::_degraded_source`) видит стопроцентную просадку и НЕ перезаписывает кеш — то есть
одна карточка чужого портала замораживает данные ВСЕХ девяти. Правильное поведение описано
в `base.py::normalize_each`.

Сценарий проверяется ЗДЕСЬ и на самой функции, а не прогоном адаптеров: копии этого теста
жили в файлах семи порталов (arbeitnow/talanto/getmatch/hirify/themuse/jobicy/web3) — семь
мест на одно поведение базового модуля, и ломает его правка одного файла.

«Кривизна» здесь — не выдуманная: это дрейф внешней схемы (скаляр там, где был список),
ровно тот класс, что дал исходный инцидент в `hirify.py::_meta_header` (`:,`-формат строки).
"""
import pytest

from hrwork.infrastructure.sources import base


class _Log:
    """Перехват строк лога: у loguru формат — str.format с позиционными аргументами."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg.format(*args))


def _card(cid: int, *, broken: bool = False) -> dict:
    """Карточка портала: id плюс поле, ставшее СКАЛЯРОМ вместо списка (дрейф схемы)."""
    return {"id": cid, "tags": 7 if broken else ["python"]}


def _normalize(it: dict) -> int:
    """Нормализация в духе адаптеров: кривое поле — исключение, у остальных id."""
    if it["tags"] == 7:
        raise TypeError("дрейф схемы: скаляр вместо списка")
    return it["id"]


def test_one_broken_card_does_not_kill_the_source(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    got = base.normalize_each([_card(1), _card(2, broken=True), _card(3)], _normalize,
                              source="web3")
    assert got == [1, 3]


@pytest.mark.parametrize("broken_at, expected", [
    (0, [2, 3]),      # первая карточка страницы
    (1, [1, 3]),      # средняя
    (2, [1, 2]),      # последняя
])
def test_a_broken_card_at_any_position_is_tolerated(broken_at, expected):
    cards = [_card(i) for i in (1, 2, 3)]
    cards[broken_at] = _card(broken_at + 1, broken=True)
    assert base.normalize_each(cards, _normalize, source="web3") == expected


def test_normalizer_returning_none_is_a_regular_drop_not_a_failure(monkeypatch):
    """`None` — штатный отсев (вакансия не подошла порталу), а не сбойная карточка:
    счётчик сбоев и warning о нём обязаны молчать."""
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)

    def _drop_second(it: dict) -> int | None:
        return None if it["id"] == 2 else it["id"]

    assert base.normalize_each([_card(1), _card(2)], _drop_second, source="web3") == [1]
    assert fake.warnings == []


def test_the_broken_card_count_is_reported_with_the_total(monkeypatch):
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    base.normalize_each([_card(1), _card(2, broken=True)], _normalize, source="web3")
    assert fake.warnings == [
        "web3: карточка пропущена (TypeError: дрейф схемы: скаляр вместо списка)",
        "web3: кривых карточек 1 из 2 — пропущены"]


def test_only_the_first_three_broken_cards_are_shown_in_detail(monkeypatch):
    """Прогон — это десятки тысяч карточек: подробная строка на каждую превратила бы лог
    в мусор, поэтому подробно показываются ровно три первые (с типом и текстом ошибки),
    дальше — только итоговый счётчик."""
    fake = _Log()
    monkeypatch.setattr(base, "log", fake)
    cards = [_card(i, broken=True) for i in range(1, 7)]
    assert base.normalize_each(cards, _normalize, source="web3") == []
    assert fake.warnings == [
        "web3: карточка пропущена (TypeError: дрейф схемы: скаляр вместо списка)"] * 3 + [
        "web3: кривых карточек 6 из 6 — пропущены"]
