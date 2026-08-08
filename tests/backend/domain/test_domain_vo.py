"""Тесты доменных Value Objects: Schedule / Experience / Salary."""
import datetime

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.freshness import FreshnessClass
from hrwork.domain.models import Vacancy
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import REMOTE_LIKE_CODES, Schedule


def _days_ago(n: int) -> str:
    return (datetime.datetime.now(tz=datetime.timezone.utc)
            - datetime.timedelta(days=n)).isoformat()


def _vac(**kw):
    base = dict(id="1", name="x", city="М", city_id="1", salary=None,
                experience=None, schedule=Schedule.OFFICE)
    return Vacancy(**{**base, **kw})


def test_schedule_from_hh_and_hirify_priority():
    assert Schedule.from_hh_formats(["REMOTE"]).hh_code == "remote"
    assert Schedule.from_hh_formats(["HYBRID"]).hh_code == "flexible"
    assert Schedule.from_hh_formats(["ON_SITE"]).hh_code == "fullDay"
    assert Schedule.from_hh_formats([]).hh_code == "fullDay"
    assert Schedule.from_hirify_wf(["remote", "hybrid"]).hh_code == "remote"   # приоритет remote
    assert Schedule.from_hirify_wf(["hybrid"]).hh_code == "flexible"
    assert Schedule.from_hirify_wf(["onsite"]).hh_code == "fullDay"
    assert Schedule.REMOTE.label == "Удалённо"


def test_schedule_from_code_is_lenient_parser():
    # единый контракт парсеров: валидный код -> VO; пусто/мусор -> None (дефолт — на вызывающем)
    assert Schedule.from_code("remote") is Schedule.REMOTE
    assert Schedule.from_code("") is None
    assert Schedule.from_code(None) is None
    assert Schedule.from_code("garbage") is None


@pytest.mark.parametrize("raw, expected", [
    ("remote", Schedule.REMOTE),
    ("hybrid", Schedule.HYBRID),
    ("office", Schedule.OFFICE),
    ("Remote", Schedule.REMOTE),      # регистр нормализует парсер, а не адаптер
    ("  hybrid  ", Schedule.HYBRID),
    ("", None),                       # поле пустое = «портал не сказал», не «офис»
    (None, None),
    ("relocation", None),             # незнакомый код не выдумываем
])
def test_schedule_from_talanto_is_lenient_parser(raw, expected):
    # мягкий контракт слово в слово как у from_code: VO | None, дефолт ставит вызывающий
    assert Schedule.from_talanto(raw) is expected


@pytest.mark.parametrize("schedule, expected", [
    (Schedule.REMOTE, True),
    (Schedule.HYBRID, True),      # гибрид — удалёнкоподобен, решение 08.08.2026
    (Schedule.OFFICE, False),
])
def test_schedule_remote_like_covers_remote_and_hybrid(schedule, expected):
    assert schedule.is_remote_like is expected


def test_remote_like_codes_are_the_single_bridge_to_js():
    # РАСХОЖДЕНИЕ 08.08.2026: analyzer считал гибрид удалёнкой, а кнопка «Офис» в
    # feed/model.js пропускала flexible в офис — 16 857 вакансий были одновременно
    # «офис» в ленте и «удалёнка» в отчётах. Коды инжектятся в feed-data.js из этой
    # константы; ожидаемое — литерал, иначе тест повторил бы реализацию.
    assert REMOTE_LIKE_CODES == ("remote", "flexible")


def test_experience_from_hirify_grades_picks_youngest():
    assert Experience.from_hirify_grades([{"name": "senior"}, {"name": "junior"}]).hh_id == "between1And3"
    assert Experience.from_hirify_grades([{"name": "trainee"}]).hh_id == "noExperience"
    assert Experience.from_hirify_grades([{"name": "lead"}]).hh_id == "moreThan6"
    assert Experience.from_hirify_grades([]) is None
    assert Experience.BETWEEN_1_3.label == "1–3 года"     # подпись из единого EXP_LABELS


def test_salary_net_and_mid():
    s = Salary.from_raw({"from": 100, "to": 200, "currency": "RUR", "gross": True})
    assert s.mid == 150
    n = s.net()
    assert (n.frm, n.to, n.mid, n.gross) == (87, 174, 130, False)     # −13% НДФЛ
    assert s.net_triple() == (87, 174, 130)


def test_salary_net_noop_when_already_net_and_none_when_empty():
    s = Salary.from_raw({"from": 500, "currency": "USD"})
    assert s.gross is False
    assert s.net() is s                                              # net -> тот же объект
    assert s.mid == 500
    assert Salary.from_raw(None) is None
    assert Salary.from_raw({"currency": "USD"}) is None              # нет вилки -> None


def test_salary_zero_bound_not_treated_as_missing():
    # from=0 — валидная граница, не «отсутствует» (is None, а не truthiness)
    s = Salary.from_raw({"from": 0, "to": 100, "currency": "RUR"})
    assert s is not None and s.mid == 50                            # (0+100)//2, не 100
    assert Salary.from_raw({"to": 0, "currency": "RUR"}).mid == 0   # одна граница = 0


@pytest.mark.parametrize("raw, expected", [
    # БАГ до 08.08.2026: net() проверял границу через truthiness (`if self.frm`), и нулевая
    # граница пропадала вместе с серединой вилки. Замер аудита: {"from":0,"to":100000,
    # "gross":true} давал (None, 87000, 87000) — медиана 87 000 вместо 43 500.
    ({"from": 0, "to": 100_000, "currency": "RUR", "gross": True}, (0, 87_000, 43_500)),
    ({"to": 0, "currency": "RUR", "gross": True}, (None, 0, 0)),      # верхняя граница = 0
    ({"from": 0, "to": 0, "currency": "RUR", "gross": True}, (0, 0, 0)),
])
def test_salary_net_keeps_zero_bound(raw, expected):
    assert Salary.from_raw(raw).net_triple() == expected


@pytest.mark.parametrize("raw, expected", [
    # net = round(gross × 0.87), а не усечение: 100 080 × 0.87 = 87 069.6 -> 87 070
    # (int() давал 87 069 — тот же класс «4166 против 4167»). Расхождение <= 1 руб.,
    # но пересчёт обязан вести себя как SalaryPeriod.to_monthly, который округляет.
    ({"from": 100_080, "currency": "RUR", "gross": True}, (87_070, None, 87_070)),
    ({"to": 250_180, "currency": "RUR", "gross": True}, (None, 217_657, 217_657)),
])
def test_salary_net_rounds_and_does_not_truncate(raw, expected):
    assert Salary.from_raw(raw).net_triple() == expected


def test_freshness_class_single_source():
    # enum — единый источник кода/подписи/цвета/порядка (закрывает split-brain 3 словарей)
    assert FreshnessClass.GHOST.code == "ghost"
    assert FreshnessClass.from_code("fresh") is FreshnessClass.FRESH
    assert FreshnessClass.FRESH.label == "Свежие (≤30 дн)"
    assert FreshnessClass.UNKNOWN.color is None                     # у «без даты» цвета нет
    assert FreshnessClass.dated() == (FreshnessClass.FRESH, FreshnessClass.RECENT,
                                      FreshnessClass.GHOST)
    # ожидаемое — литералом, а не `all(...)` и не `order == sorted(order)`: обобщённый флаг
    # проходил на любом наборе цветов, а самосравнение — на любом уже отсортированном порядке
    assert [fc.color for fc in FreshnessClass.dated()] == ["#4C9BD1", "#BB8B31", "#D64550"]
    assert [fc.order for fc in FreshnessClass] == [0, 1, 2, 3]      # порядок = порядок членов


# ── Поведение сущности Vacancy (F2a-behaviour): даты/формат -> методы ──
def test_vacancy_freshness_methods():
    fresh = _vac(created_at=_days_ago(5))
    assert fresh.age_days() == 5 and fresh.fresh_class() is FreshnessClass.FRESH and not fresh.is_ghost()
    ghost = _vac(created_at=_days_ago(90))
    assert ghost.fresh_class() is FreshnessClass.GHOST and ghost.is_ghost()
    recent = _vac(created_at=_days_ago(45))
    assert recent.fresh_class() is FreshnessClass.RECENT and not recent.is_ghost()
    undated = _vac(created_at=None)
    assert undated.age_days() is None and undated.fresh_class() is FreshnessClass.UNKNOWN


def test_vacancy_republish_gap():
    v = _vac(created_at=_days_ago(30), published_at=_days_ago(2))   # переоткрыта позже создания
    assert v.republish_gap_days() == 28
    assert _vac(created_at=_days_ago(10)).republish_gap_days() is None  # нет published -> None


def test_vacancy_is_remote_strict():
    assert _vac(schedule=Schedule.REMOTE).is_remote() is True
    assert _vac(schedule=Schedule.HYBRID).is_remote() is False       # гибрид — не строгий remote
    assert _vac(schedule=Schedule.OFFICE).is_remote() is False


@pytest.mark.parametrize("schedule, expected", [
    (Schedule.REMOTE, True),
    (Schedule.HYBRID, True),      # то, что срезы и лента называют «удалённо»
    (Schedule.OFFICE, False),
])
def test_vacancy_is_remote_like_counts_hybrid(schedule, expected):
    assert _vac(schedule=schedule).is_remote_like() is expected


def test_vacancy_without_schedule_is_not_remote_like():
    # формата нет -> НЕ удалёнка: домыслить её из пустого поля значило бы завысить срез,
    # а вакансия прошла бы удалённый фильтр отбора под отклик
    assert _vac(schedule=None).is_remote_like() is False
