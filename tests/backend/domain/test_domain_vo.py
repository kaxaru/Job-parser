"""Тесты доменных Value Objects: Schedule / Experience / Salary."""
import datetime

from hrwork.domain.experience import Experience
from hrwork.domain.freshness import FreshnessClass
from hrwork.domain.models import Vacancy
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule


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
    assert s.gross is False and s.net() is s and s.mid == 500        # net -> тот же объект
    assert Salary.from_raw(None) is None
    assert Salary.from_raw({"currency": "USD"}) is None              # нет вилки -> None


def test_salary_zero_bound_not_treated_as_missing():
    # from=0 — валидная граница, не «отсутствует» (is None, а не truthiness)
    s = Salary.from_raw({"from": 0, "to": 100, "currency": "RUR"})
    assert s is not None and s.mid == 50                            # (0+100)//2, не 100
    assert Salary.from_raw({"to": 0, "currency": "RUR"}).mid == 0   # одна граница = 0


def test_freshness_class_single_source():
    # enum — единый источник кода/подписи/цвета/порядка (закрывает split-brain 3 словарей)
    assert FreshnessClass.GHOST.code == "ghost"
    assert FreshnessClass.from_code("fresh") is FreshnessClass.FRESH
    assert FreshnessClass.FRESH.label == "Свежие (≤30 дн)"
    assert FreshnessClass.UNKNOWN.color is None                     # у «без даты» цвета нет
    assert all(fc.color for fc in FreshnessClass.dated())           # у датированных цвет есть
    assert FreshnessClass.dated() == (FreshnessClass.FRESH, FreshnessClass.RECENT,
                                      FreshnessClass.GHOST)
    order = [fc.order for fc in FreshnessClass]
    assert order == sorted(order) and order[0] == 0                 # порядок = порядок членов


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
