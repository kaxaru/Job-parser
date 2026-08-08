"""Тесты агрегатора статистики (Analyzer) на in-memory вакансиях — без сети/БД."""
import datetime

import pytest

from hrwork.application.analyzer import Analyzer
from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.net import rates

# Фиксируем FX-курсы (per-USD) — тесты гермётичны, без сети. USD 1:1, RUB 100, EUR 0.9.
_FX = {"USD": 1.0, "RUB": 100.0, "EUR": 0.9}


@pytest.fixture(autouse=True)
def _stub_rates(monkeypatch):
    monkeypatch.setattr(rates, "get_rates", lambda: dict(_FX))


def _vac(vid, city, mid, currency="RUR", exp="between1And3", schedule="fullDay",
         techs=(), created_at=None, employer="", role=Role.DEVELOPER, source="hh"):
    return Vacancy(
        id=vid, name="x", city=city, city_id="1",
        salary=Salary(mid, None, currency) if mid is not None else None,
        experience=Experience.from_code(exp), schedule=Schedule.from_code(schedule),
        techs=list(techs), role=role, employer=employer, created_at=created_at,
        source=source,
    )


def _days_ago(n: int) -> str:
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    return (now - datetime.timedelta(days=n)).isoformat()


def test_count_by_city():
    a = Analyzer.with_live_rates([_vac("1", "Москва", 100), _vac("2", "Москва", 100), _vac("3", "СПб", 100)])
    c = a.count_by_city()
    assert c["Москва"] == 2
    assert c["СПб"] == 1


def test_paid_normalizes_all_currencies_to_rub():
    a = Analyzer.with_live_rates([
        _vac("1", "Москва", 100, currency="RUR"),    # 100 RUB
        _vac("2", "Москва", 100, currency="USD"),    # 100 USD -> 10000 RUB (курс 100)
        _vac("3", "Москва", None, currency="RUR"),   # нет зарплаты -> выпадает
    ])
    assert len(a.paid) == 2                          # hirify(USD) больше НЕ выкидывается
    by_id = {v.id: rub for v, rub in a.paid}         # paid — (Vacancy, rub), entity НЕ мутируется
    assert by_id["1"] == 100                         # RUR как есть
    assert by_id["2"] == 10000                       # USD -> RUB по курсу


def test_remote_by_city_counts_remote_and_flexible():
    a = Analyzer.with_live_rates([
        _vac("1", "Москва", 100, schedule="remote"),
        _vac("2", "Москва", 100, schedule="fullDay"),
        _vac("3", "Москва", 100, schedule="flexible"),   # flexible тоже считается remote
    ])
    row = a.remote_by_city()[0]
    assert row["city"] == "Москва"
    assert (row["total"], row["remote"], row["onsite"]) == (3, 2, 1)
    assert row["pct"] == round(2 * 100 / 3, 1)


# --- «Сколько удалёнки» — ОДИН предикат на весь проект (домен), а не кортеж в аналитике ---
# 08.08.2026: `analyzer._REMOTE` отвечал на этот вопрос сам, а лента — своим фильтром, и
# 16 857 вакансий были «офисом» в ленте и «удалёнкой» в отчётах 09/11/12. Победил домен
# (`Vacancy.is_remote_like`). Семантика срезов при этом НЕ менялась — это и фиксируется:
# remote и гибрид считаются удалёнкой, офис и «формат не назван» — нет.

@pytest.mark.parametrize(("schedule", "remote", "onsite"), [
    ("remote", 1, 0),
    ("flexible", 1, 0),      # гибрид = удалёнка (был им и до перевода на доменный предикат)
    ("fullDay", 0, 1),
    (None, 0, 1),            # портал формат не назвал -> удалёнку не домысливаем
])
def test_remote_by_city_counts_remote_and_hybrid_as_remote(schedule, remote, onsite):
    row = Analyzer.with_live_rates([_vac("1", "Москва", 100, schedule=schedule)]).remote_by_city()[0]
    assert (row["total"], row["remote"], row["onsite"]) == (1, remote, onsite)
    assert row["pct"] == float(remote * 100)


@pytest.mark.parametrize(("schedule", "remote", "office"), [
    ("remote", 1, 0),
    ("flexible", 1, 0),
    ("fullDay", 0, 1),
    (None, 0, 1),
])
def test_by_company_remote_split_matches_domain_predicate(schedule, remote, office):
    row = Analyzer.with_live_rates(
        [_vac("1", "М", 100, employer="Acme", schedule=schedule)]).by_company()[0]
    assert (row["remote"], row["office"]) == (remote, office)


@pytest.mark.parametrize(("schedule", "remote", "office"), [
    ("remote", 1, 0),
    ("flexible", 1, 0),
    ("fullDay", 0, 1),
    (None, 0, 1),
])
def test_by_source_remote_split_matches_domain_predicate(schedule, remote, office):
    row = Analyzer.with_live_rates(
        [_vac("1", "М", 100, source="hirify", schedule=schedule)]).by_source()[0]
    assert (row["remote"], row["office"]) == (remote, office)


# --- Агрегаты округляются, а не усекаются (класс инцидента «4166 против 4167») ---

def test_salary_by_lang_rounds_mean_and_quartiles():
    # БАГ 08.08.2026: int() усекал среднее и квантили. Тот же класс, что `salary.py::net`
    # («4166 там, где верно 4167»): цена копеечная, но один и тот же пересчёт обязан
    # вести себя одинаково во всём проекте.
    # Вилка n=5: 100001, 100002, 100002, 100003, 100005 (net RUB, курс 1:1).
    vacs = [_vac(str(i), "Москва", s, techs=["Python"])
            for i, s in enumerate((100_001, 100_002, 100_002, 100_003, 100_005))]
    out = Analyzer.with_live_rates(vacs).salary_by_lang()["Python"]
    assert out["n"] == 5
    assert out["mean"] == 100_003        # 500 013 / 5 = 100 002.6 -> 100 003 (усечение: 100 002)
    assert out["median"] == 100_002
    assert out["p25"] == 100_002         # 100 001.5 -> 100 002 (усечение: 100 001)
    assert out["p75"] == 100_004
    assert (out["min"], out["max"]) == (100_001, 100_005)


def test_salary_city_lang_rounds_median_and_quartiles():
    # ячейка город×язык: порог 3, квантили считаются от 4 значений
    # 100000, 100001, 100002, 100003 -> медиана 100 001.5, P75 100 002.75
    vacs = [_vac(str(i), "Москва", s, techs=["Python"])
            for i, s in enumerate((100_000, 100_001, 100_002, 100_003))]
    cell = Analyzer.with_live_rates(vacs).salary_city_lang()["Москва"]["Python"]
    assert cell["n"] == 4
    assert cell["median"] == 100_002     # 100 001.5 -> 100 002 (усечение: 100 001)
    assert cell["p25"] == 100_000
    assert cell["p75"] == 100_003        # 100 002.75 -> 100 003 (усечение: 100 002)


def test_salary_by_experience_rounds_quartiles():
    vacs = [_vac(str(i), "Москва", s, exp="between1And3")
            for i, s in enumerate((100_001, 100_002, 100_002, 100_003, 100_005))]
    out = Analyzer.with_live_rates(vacs).salary_by_experience()["1–3 года"]
    assert out["median"] == 100_002
    assert out["p25"] == 100_002         # 100 001.5 -> 100 002 (усечение: 100 001)
    assert out["p75"] == 100_004


@pytest.mark.parametrize("read", [
    lambda a: a.freshness_summary()["median_age"],
    lambda a: a.by_company()[0]["median_age"],
    lambda a: a.freshness_by_city()[0]["median_age"],
], ids=["freshness_summary", "by_company", "freshness_by_city"])
def test_median_age_rounds_instead_of_truncating(read):
    # медиана возраста 11 и 12 дней = 11.5 -> 12 дней, а не 11 (int усекал)
    a = Analyzer.with_live_rates([
        _vac("1", "М", 100, employer="Acme", created_at=_days_ago(11)),
        _vac("2", "М", 100, employer="Acme", created_at=_days_ago(12)),
    ])
    assert read(a) == 12


def test_salary_by_experience_threshold_and_median():
    vacs = [_vac(str(i), "Москва", 100000 + i * 10000, exp="between1And3") for i in range(5)]
    out = Analyzer.with_live_rates(vacs).salary_by_experience()
    key = next(iter(out))                  # метка EXP_LABELS['between1And3']
    assert out[key]["n"] == 5
    assert out[key]["median"] == 120000    # median(100..140k)


def test_salary_by_experience_below_threshold_excluded():
    vacs = [_vac(str(i), "Москва", 100000, exp="moreThan6") for i in range(4)]  # <5
    assert Analyzer.with_live_rates(vacs).salary_by_experience() == {}


def test_salary_by_lang_requires_lang_key_and_threshold():
    vacs = [_vac(str(i), "Москва", 100000 + i * 1000, techs=["Python"]) for i in range(5)]
    out = Analyzer.with_live_rates(vacs).salary_by_lang()
    assert "Python" in out
    assert out["Python"]["n"] == 5


def test_freshness_summary_counts_and_ghost_pct():
    a = Analyzer.with_live_rates([
        _vac("1", "М", 100, created_at=_days_ago(5)),    # fresh
        _vac("2", "М", 100, created_at=_days_ago(45)),   # recent
        _vac("3", "М", 100, created_at=_days_ago(90)),   # ghost
        _vac("4", "М", 100, created_at=_days_ago(80)),   # ghost
        _vac("5", "М", 100, created_at=None),            # unknown (нет даты)
    ])
    s = a.freshness_summary()
    assert s["counts"] == {"fresh": 1, "recent": 1, "ghost": 2, "unknown": 1}
    assert s["dated"] == 4                                # без unknown
    assert s["ghost_pct"] == 50.0                         # 2/4
    assert s["fresh_pct"] == 25.0


def test_freshness_by_city_sorted_by_ghost_share():
    a = Analyzer.with_live_rates([
        _vac("1", "Гост-сити", 100, created_at=_days_ago(90)),
        _vac("2", "Гост-сити", 100, created_at=_days_ago(70)),
        _vac("3", "Свеж-сити", 100, created_at=_days_ago(5)),
        _vac("4", "Нет-дат", 100, created_at=None),       # город без дат -> выпадает
    ])
    rows = a.freshness_by_city()
    assert [r["city"] for r in rows] == ["Гост-сити", "Свеж-сити"]   # сорт по ghost_pct
    assert rows[0]["ghost_pct"] == 100.0
    assert rows[1]["fresh"] == 1


def test_by_company_counts_and_median_age():
    a = Analyzer.with_live_rates([
        _vac("1", "М", 100, employer="Acme", created_at=_days_ago(10)),
        _vac("2", "М", 100, employer="Acme", created_at=_days_ago(90)),
        _vac("3", "М", 100, employer="Solo", created_at=_days_ago(5)),
        _vac("4", "М", 100, employer=""),          # без компании -> не считаем
    ])
    rows = a.by_company()
    assert [r["company"] for r in rows] == ["Acme", "Solo"]   # сорт по числу вакансий
    assert rows[0]["total"] == 2
    assert rows[0]["median_age"] == 50            # median(10, 90)
    assert rows[0]["ghosts"] == 1                 # вакансия 90 дн — гост
    assert rows[1]["total"] == 1


def test_by_company_remote_office_split():
    a = Analyzer.with_live_rates([
        _vac("1", "М", 100, employer="Acme", schedule="remote"),
        _vac("2", "М", 100, employer="Acme", schedule="flexible"),   # flexible = удалёнка
        _vac("3", "М", 100, employer="Acme", schedule="fullDay"),
    ])
    r = a.by_company()[0]
    assert (r["remote"], r["office"]) == (2, 1)


def test_companies_by_size_buckets_and_freshness():
    # корзины по числу вакансий; стек — класс свежести компании по медиане возраста
    vacs = ([_vac("a", "Москва", 100_000, employer="Одиночка", created_at=_days_ago(5))]
            + [_vac(f"b{i}", "Москва", 100_000, employer="Пара", created_at=_days_ago(45))
               for i in range(2)]
            + [_vac(f"c{i}", "Москва", 100_000, employer="Крупный", created_at=_days_ago(200))
               for i in range(7)])
    rows = {r["bucket"]: r for r in Analyzer(vacs, fx=dict(_FX)).companies_by_size()}
    assert [r["bucket"] for r in Analyzer(vacs, fx=dict(_FX)).companies_by_size()] \
        == ["1", "2", "3", "4", "5–10", "11–25", "26–50", "51–100", "100+"]  # все, даже пустые
    assert (rows["1"]["companies"], rows["1"]["fresh"]) == (1, 1)      # <=30 дн -> свежая
    assert (rows["2"]["companies"], rows["2"]["recent"]) == (1, 1)     # 30-60 -> средняя
    assert (rows["5–10"]["companies"], rows["5–10"]["ghost"]) == (1, 1)  # 7 вакансий, >60 -> гост
    assert rows["5–10"]["vacancies"] == 7                # вакансии тоже считаем
    assert rows["5–10"]["ghost_pct"] == 100.0
    assert rows["100+"]["companies"] == 0                # пустая корзина не ломает график


def test_companies_by_size_excludes_non_it():
    # тот же фильтр, что и в by_company: ритейл-мусор не раздувает хвост
    vacs = [_vac("1", "Москва", 100_000, employer="Ритейл", role=Role.NON_IT,
                 created_at=_days_ago(5))]
    # Ожидаемое литералом по спеке SIZE_BUCKETS, а не `all(...)`: обобщённый флаг не называл
    # виновную корзину и проходил даже на ПУСТОМ списке корзин.
    rows = Analyzer(vacs, fx=dict(_FX)).companies_by_size()
    assert [r["bucket"] for r in rows] == ["1", "2", "3", "4", "5–10", "11–25", "26–50",
                                           "51–100", "100+"]
    assert [r["companies"] for r in rows] == [0, 0, 0, 0, 0, 0, 0, 0, 0]


def test_by_company_excludes_non_it():
    # ритейл (роль «Не-IT») не должен раздувать счётчик компании (кейс Fix Price)
    a = Analyzer.with_live_rates([
        _vac("1", "М", 100, employer="Fix Price", role=Role.BACKEND),
        _vac("2", "М", 100, employer="Fix Price", role=Role.NON_IT),
        _vac("3", "М", 100, employer="Fix Price", role=Role.NON_IT),
    ])
    rows = a.by_company()
    assert rows[0]["total"] == 1                   # только IT-вакансия, 2 не-IT отброшены


def test_freshness_by_city_has_recent_band():
    a = Analyzer.with_live_rates([
        _vac("1", "М", 100, created_at=_days_ago(5)),    # fresh
        _vac("2", "М", 100, created_at=_days_ago(45)),   # recent (30–60)
        _vac("3", "М", 100, created_at=_days_ago(90)),   # ghost
    ])
    row = a.freshness_by_city()[0]
    assert (row["fresh"], row["recent"], row["ghost"]) == (1, 1, 1)


def test_tech_stack_for_lang_cooccurrence():
    vacs = [
        _vac("1", "М", 100, techs=["Python", "Docker"]),
        _vac("2", "М", 100, techs=["Python", "Docker", "Redis"]),
        _vac("3", "М", 100, techs=["Java"]),
    ]
    stack = dict(Analyzer.with_live_rates(vacs).tech_stack_for_lang("Python"))
    assert stack["Docker"] == 2
    assert stack["Redis"] == 1
    assert "Java" not in stack


def test_by_source_splits_by_portal():
    def mk(vid, src, sched="remote"):
        return Vacancy(id=vid, name="x", city="М", city_id="1",
                       salary=Salary(100, None, "RUR"),
                       experience=Experience.from_code("between1And3"),
                       schedule=Schedule.from_code(sched), source=src)
    rows = Analyzer.with_live_rates([mk("1", "hh"), mk("2", "hh"), mk("3", "hirify", "fullDay")]).by_source()
    assert rows[0]["source"] == "hh" and rows[0]["total"] == 2      # сорт по числу вакансий
    assert rows[0]["remote"] == 2
    hf = next(r for r in rows if r["source"] == "hirify")
    assert hf["total"] == 1 and hf["office"] == 1
