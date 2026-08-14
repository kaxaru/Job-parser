"""Адаптеры ATS работодателей: greenhouse.io и ashbyhq.com.

Оба источника устроены не как доски объявлений: единой выдачи нет, есть борд каждой
компании по слагу, и охват задаёт наш реестр. Здесь проверяется только маппинг внешней
схемы в домен (ACL) и общая механика реестра — сеть не трогается.

Инварианты, ради которых тесты и написаны (все три — свойства РЕАЛЬНОЙ выдачи, снятой
13.08.2026, а не выдуманные случаи):
  * greenhouse отдаёт описание ДВАЖДЫ экранированным (`&lt;p&gt;` вместо `<p>`), и снятие
    тегов до `unescape` не находит ни одного тега — весь детект стека уехал бы в ноль;
  * ashby ставит `isRemote: true` в том числе при `workplaceType: Hybrid`, поэтому формат
    работы обязан определяться по workplaceType, иначе гибрид уедет в удалёнку;
  * ashby отдаёт интервал вилки как «1 YEAR», а рядом с зарплатным компонентом лежит
    компонент опционов без валюты и сумм.
"""
import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.models import REMOTE_CITY
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.sources import ashby, greenhouse
from hrwork.infrastructure.sources.ats import pretty_company

pytestmark = pytest.mark.usefixtures("apply_defaults")


def _gh(**kw):
    base = {
        "id": 8556658002,
        "title": "Senior Python Engineer",
        "company_name": "GitLab",
        "location": {"name": "Remote, Bangalore"},
        "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/8556658002",
        "first_published": "2026-05-22T09:16:29-04:00",
        "updated_at": "2026-08-11T08:18:18-04:00",
        "departments": [{"name": "Engineering"}],
        # экранирование ДВОЙНОЕ — ровно как приходит с портала
        "content": "&lt;p&gt;We use Python, FastAPI and PostgreSQL.&lt;/p&gt;",
    }
    return {**base, **kw}


def _ash(**kw):
    base = {
        "id": "d3bc1ced-3ce4-4086-a050-555055dbb1ff",
        "title": "Senior Fullstack Engineer",
        "location": "Europe",
        "isRemote": True,
        "workplaceType": "Remote",
        "employmentType": "FullTime",
        "publishedAt": "2026-04-27T20:13:45.158+00:00",
        "department": "Engineering",
        "team": "Platform",
        "descriptionPlain": "TypeScript, React and PostgreSQL.",
        "descriptionHtml": "<p>TypeScript, React and PostgreSQL.</p>",
        "jobUrl": "https://jobs.ashbyhq.com/linear/d3bc1ced",
        "compensation": {"compensationTiers": [{"components": [
            {"compensationType": "EquityPercentage", "interval": "NONE",
             "currencyCode": None, "minValue": None, "maxValue": None},
            {"compensationType": "Salary", "interval": "1 YEAR",
             "currencyCode": "USD", "minValue": 211400, "maxValue": 290600},
        ]}]},
    }
    return {**base, **kw}


# ── greenhouse ─────────────────────────────────────────────────────────────────

def test_greenhouse_maps_card_to_domain():
    r = greenhouse._normalize(_gh())
    v = r.vacancy
    assert v.id == "greenhouse_8556658002"
    assert v.name == "Senior Python Engineer"
    assert v.employer == "GitLab"
    assert v.city == "Remote, Bangalore"
    assert v.source == "greenhouse"
    assert r.url == "https://job-boards.greenhouse.io/gitlab/jobs/8556658002"


def test_greenhouse_double_escaped_content_becomes_plain_text():
    """Снятие тегов ДО unescape не нашло бы ни одного тега: в JSON лежит не HTML,
    а его entity-представление. Тогда `requirement` был бы засорён `&lt;p&gt;`,
    а детект стека — пустым."""
    r = greenhouse._normalize(_gh())
    assert r.requirement == "We use Python, FastAPI and PostgreSQL."
    assert r.vacancy.techs == ["Python", "FastAPI", "PostgreSQL"]


@pytest.mark.parametrize("location, expected", [
    ({"name": "Remote, Italy"}, Schedule.REMOTE),
    ({"name": "Remote - Americas"}, Schedule.REMOTE),
    ({"name": "San Francisco"}, Schedule.OFFICE),
    ({"name": ""}, Schedule.REMOTE),          # пустой город -> REMOTE_CITY -> удалёнка
])
def test_greenhouse_schedule_comes_from_location_string(location, expected):
    assert greenhouse._normalize(_gh(location=location)).vacancy.schedule is expected


def test_greenhouse_empty_location_uses_the_shared_remote_label():
    assert greenhouse._normalize(_gh(location={"name": ""})).vacancy.city == REMOTE_CITY


def test_greenhouse_has_no_salary_or_grade():
    # вилки и грейда в схеме борда нет — выдумывать нельзя, поедут срезы аналитики
    v = greenhouse._normalize(_gh()).vacancy
    assert v.salary is None
    assert v.experience is None


def test_greenhouse_age_counts_from_first_publication_not_from_edit():
    """created_at = first_published: правка описания не должна молодить старую вакансию.
    updated_at при этом работает маркером изменения карточки."""
    r = greenhouse._normalize(_gh())
    assert r.vacancy.created_at == "2026-05-22T09:16:29-04:00"
    assert r.sig == "2026-08-11T08:18:18-04:00"


# ── ashby ──────────────────────────────────────────────────────────────────────

def test_ashby_maps_card_to_domain():
    r = ashby._normalize(_ash(), org="linear")
    v = r.vacancy
    assert v.id == "ashby_d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    assert v.name == "Senior Fullstack Engineer"
    assert v.city == "Europe"
    assert v.source == "ashby"
    assert r.url == "https://jobs.ashbyhq.com/linear/d3bc1ced"


def test_ashby_employer_comes_from_board_slug():
    """Имени компании в схеме нет вовсе, а работодатель — половина ключа кросс-портального
    дедупа. Единственный источник имени — слаг борда."""
    assert ashby._normalize(_ash(), org="cockroach-labs").vacancy.employer == "Cockroach Labs"


@pytest.mark.parametrize("workplace, expected", [
    ("Remote", Schedule.REMOTE),
    ("Hybrid", Schedule.HYBRID),
    ("OnSite", Schedule.OFFICE),
    ("", Schedule.OFFICE),                    # неизвестное -> доменный дефолт
])
def test_ashby_schedule_comes_from_workplace_type(workplace, expected):
    v = ashby._normalize(_ash(workplaceType=workplace), org="linear").vacancy
    assert v.schedule is expected


def test_ashby_hybrid_is_not_promoted_to_remote_by_the_is_remote_flag():
    """`isRemote: true` портал ставит и гибриду (ramp, 13.08.2026: isRemote=true,
    workplaceType=Hybrid, location «New York, NY (HQ)»). Поверить флагу значит записать
    офисный гибрид в удалёнку — а по этому полю лента отбирает, куда можно откликаться
    из другой страны."""
    v = ashby._normalize(_ash(isRemote=True, workplaceType="Hybrid"), org="ramp").vacancy
    assert v.schedule is Schedule.HYBRID


def test_ashby_salary_comes_from_structured_component_as_monthly():
    """211 400-290 600 USD в год -> месячные /12. Считано отдельно: 211400/12 = 17616.7,
    290600/12 = 24216.7."""
    s = ashby._normalize(_ash(), org="ramp").vacancy.salary
    assert (s.frm, s.to, s.currency) == (17617, 24217, "USD")


def test_ashby_equity_component_is_not_taken_as_salary():
    """Рядом с зарплатой лежит компонент опционов — без валюты и сумм. Сложить их в вилку
    было бы враньём, поэтому берётся только compensationType == Salary."""
    only_equity = {"compensationTiers": [{"components": [
        {"compensationType": "EquityPercentage", "interval": "NONE",
         "currencyCode": None, "minValue": None, "maxValue": None}]}]}
    assert ashby._normalize(_ash(compensation=only_equity), org="ramp").vacancy.salary is None


def test_ashby_unknown_interval_drops_the_range():
    # множитель, отличный от единицы, портал не отдавал; ошибка в периоде хуже пустого поля
    comp = {"compensationTiers": [{"components": [
        {"compensationType": "Salary", "interval": "2 YEAR",
         "currencyCode": "USD", "minValue": 100, "maxValue": 200}]}]}
    assert ashby._normalize(_ash(compensation=comp), org="ramp").vacancy.salary is None


def test_ashby_foreign_salary_is_not_taxed_as_russian():
    assert ashby._normalize(_ash(), org="ramp").vacancy.salary.gross is False


@pytest.mark.parametrize("employment_type, expected", [
    ("Intern", Experience.NONE),
    ("FullTime", None),
    ("Contract", None),
    ("", None),
])
def test_ashby_employment_type_gives_grade_only_for_internship(employment_type, expected):
    """Грейда у портала нет; из набора FullTime/Contract/Intern доменная таблица грейдов
    узнаёт только стажировку. Остальное — None, а не выдуманный уровень."""
    v = ashby._normalize(_ash(employmentType=employment_type), org="linear").vacancy
    assert v.experience is expected


def test_ashby_employment_type_does_not_become_a_contract_form():
    """`employmentType` — ДРУГАЯ ось, чем `Employment` (ТК/СЗ/ИП/ГПХ). FullTime в США
    не является трудовым договором по ТК РФ, и записать его туда значило бы засорить
    срез оформления зарубежными вакансиями."""
    v = ashby._normalize(_ash(employmentType="Contract"), org="linear").vacancy
    assert v.employment == ()


# ── общий реестр бордов ────────────────────────────────────────────────────────

@pytest.mark.parametrize("slug, expected", [
    ("gitlab", "Gitlab"),
    ("cockroach-labs", "Cockroach Labs"),
    ("temporal_technologies", "Temporal Technologies"),
    ("", ""),
])
def test_board_slug_becomes_readable_company_name(slug, expected):
    assert pretty_company(slug) == expected
