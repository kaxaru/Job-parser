"""Структурные тесты сборки ленты (build_feed) -> payload feed-data.js.

Отдельно от test_sanitize.py (тот про XSS-санитайзер): здесь проверяем, что сборщик
эмитит ожидаемые JS-глобалы и что карточные поля вакансии переживают round-trip
Python -> JSON -> feed-data.js. Всё in-memory: репозиторий, store, FX-курсы и esbuild
подменены, поэтому ни сети, ни браузера, ни записи в реальный data/.
"""
import json

import pytest

from hrwork.domain.experience import Experience
from hrwork.domain.models import Vacancy
from hrwork.domain.role import Role
from hrwork.domain.salary import Salary
from hrwork.domain.schedule import Schedule
from hrwork.infrastructure.storage import VacancyRecord, followup
from hrwork.presentation.views import feed

# FX-курсы (per-USD) фиксируем -> тесты гермётичны, без сети. USD 1:1, RUB 100.
_FX = {"USD": 1.0, "RUB": 100.0, "EUR": 0.9}
_MARKS = {"v1": "applied"}
_STATUSES = {"v1": "INVITATION"}     # у v1 есть статус работодателя из чата
_FORMS = {"v2": {"name": "Аналитик данных"}}   # v2 — вакансия-опросник (нужна форма)
# v1 — форма протухла при свипе (status=empty) -> карточка гасится как «не актуальна»
_FORM_CACHE = {"v1": {"status": "empty"}, "v2": {"status": "ok"}}
# Переписка по v1: живой ответ HR — последний НЕ наш и НЕ ботовый (04.07 11:00). Бот пишет
# позже (10.07), наше сообщение — между: оба в `hr_ts` попадать не должны.
_CHATS = {
    "v1": {"messages": [
        {"text": "Ваш отклик зарегистрирован", "ts": "2026-07-10T08:00:00+03:00", "bot": True},
        {"text": "Здравствуйте! Расскажете о себе?", "ts": "2026-07-02T09:00:00+03:00"},
        {"text": "Да, конечно", "ts": "2026-07-03T10:00:00+03:00", "mine": True},
        {"text": "Когда вам удобно созвониться?", "ts": "2026-07-04T11:00:00+03:00"},
    ]},
}


def _live_crm_tripwire(*_args, **_kwargs):
    """Растяжка на ЖИВОЕ CRM-состояние: сборка ленты обязана читать только подменённое.

    ИНЦИДЕНТ-КЛАСС «второй держатель зависимости» (аудит 08.08.2026, находка 59): фикстура
    подменяла четыре метода `store`, а `feed.py::_last_hr_replies` ходил в пятый —
    `store.chat_messages()`, — и поле `hr_ts` считалось из переписки ЗАПУСКАЮЩЕГО."""
    raise AssertionError("build_feed прочитал живой data/chat_messages.json — "
                         "подмена feed.store.chat_messages потеряна")


class _Repo:
    """Заглушка vacancy_repository: отдаёт готовые записи вместо чтения файла."""

    def __init__(self, records):
        self._records = records

    def load(self):
        return self._records


def _record(vid, *, name, city, employer, source, mid, currency, exp, schedule,
            techs, role, url, desc="", req=""):
    v = Vacancy(
        id=vid, name=name, city=city, city_id="1",
        salary=Salary(mid, None, currency) if mid is not None else None,
        experience=Experience.from_code(exp),
        schedule=Schedule.from_code(schedule) or Schedule.OFFICE,
        techs=list(techs), role=role, employer=employer,
        created_at="2026-06-01T00:00:00+00:00", responses=7 if vid == "v1" else None,
        source=source,
    )
    return VacancyRecord(vacancy=v, url=url, description_html=desc, requirement=req)


def _records():
    return [
        # remote -> remote_any=True; есть статус, нет формы; резюме-валюта RUR
        _record("v1", name="Python Backend", city="Москва", employer="Acme",
                source="hh", mid=200_000, currency="RUR", exp="between1And3",
                schedule="remote", techs=["Python", "Docker"], role=Role.BACKEND,
                url="https://hh.ru/vacancy/v1", desc="<p>Описание</p>", req="Требования"),
        # office + текст без маркеров -> remote_any=False; нет статуса, нужна форма; USD
        _record("v2", name="Аналитик данных", city="Санкт-Петербург", employer="Gamma",
                source="hirify", mid=3_000, currency="USD", exp="between3And6",
                schedule="fullDay", techs=["SQL"], role=Role.ANALYST,
                url="https://hirify.io/v2", desc="", req="Офисная работа"),
    ]


def _const(js_text: str, name: str):
    """Достать значение JS-константы `const NAME = <json>;` из feed-data.js -> Python-объект.
    json.dumps(ensure_ascii=False) не эмитит переносов -> каждая константа на своей строке."""
    prefix = f"const {name} = "
    for line in js_text.splitlines():
        if line.startswith(prefix):
            payload = line[len(prefix):]
            assert payload.endswith(";"), f"строка {name} без завершающей ;"
            return json.loads(payload[:-1])
    raise KeyError(name)


def _build_feed_files(out_dir, records=None, chats=None, **config_overrides):
    """Собрать ленту в `out_dir` с подменёнными зависимостями -> (feed-data.js, feed.html).

    Свой MonkeyPatch-контекст: фикстура `monkeypatch` функциональная и в модульную не годится.
    `config_overrides` — атрибуты модуля `feed` (константы конфига), чтобы проверять инжекцию
    настроек профиля, не трогая файл на диске.

    Подменяются ВСЕ пять держателей CRM-состояния (`store.statuses/forms/form_cache/marks/
    chat_messages`) плюс растяжка на настоящий загрузчик переписки: `store.chat_messages` —
    статический делегат в `followup.load_chat_messages`, и патч надо ставить у ТОГО
    держателя, через которого идёт вызов (`feed.store`), а растяжка ловит промах."""
    data = out_dir / "data"
    records = _records() if records is None else records
    chats = {} if chats is None else chats
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(feed, "vacancy_repository", lambda: _Repo(records))
        mp.setattr(feed.store, "statuses", lambda: dict(_STATUSES))
        mp.setattr(feed.store, "forms", lambda: dict(_FORMS))
        mp.setattr(feed.store, "form_cache", lambda: dict(_FORM_CACHE))
        mp.setattr(feed.store, "marks", lambda: dict(_MARKS))
        mp.setattr(feed.store, "chat_messages", lambda: dict(chats))
        mp.setattr(followup, "load_chat_messages", _live_crm_tripwire)
        mp.setattr(feed.rates, "get_rates", lambda: dict(_FX))
        mp.setattr(feed.shutil, "which", lambda _name: None)   # esbuild off -> не собираем feed.js
        mp.setattr(feed, "DATA_DIR", data)
        mp.setattr(feed, "FEED_OUT", data / "feed.html")
        for name, value in config_overrides.items():
            mp.setattr(feed, name, value)
        feed.build_feed()
        return ((data / "feed-data.js").read_text(encoding="utf-8"),
                (data / "feed.html").read_text(encoding="utf-8"))


def _build_feed_data(out_dir, records=None, chats=None, **config_overrides):
    """Только текст feed-data.js (большинству тестов HTML не нужен)."""
    return _build_feed_files(out_dir, records, chats, **config_overrides)[0]


@pytest.fixture(scope="module")
def _feed_bundle(tmp_path_factory):
    """Текст feed-data.js: `build_feed` дорогой, поэтому собирается один раз на модуль."""
    return _build_feed_data(tmp_path_factory.mktemp("feed"), chats=_CHATS)


@pytest.fixture
def feed_globals(_feed_bundle):
    """Разобранный feed-data.js — СВОИ объекты на каждый тест.

    Раньше фикстура была модульной и раздавала одни и те же словари 40+ тестам: тест,
    поправивший карточку «чтобы проверить», молча менял вход соседям, а зависимость от
    порядка делала такую поломку невоспроизводимой (аудит 08.08.2026, находка 64).
    Разбор JSON заново стоит доли миллисекунды, сборка ленты — нет."""
    vac_list = _const(_feed_bundle, "VACANCIES")
    return {
        "text": _feed_bundle,
        "vacancies_by_id": {v["id"]: v for v in vac_list},
        "sal_max": _const(_feed_bundle, "SAL_MAX"),
        "saved_marks": _const(_feed_bundle, "SAVED_MARKS"),
        "fx_rates": _const(_feed_bundle, "FX_RATES"),
        "fx_alias": _const(_feed_bundle, "FX_ALIAS"),
    }


@pytest.mark.parametrize("case", ["портит карточку соседу", "обязан получить чистую"])
def test_feed_globals_are_rebuilt_for_every_test(case, feed_globals):
    """Каждый тест получает СВОИ словари, а не общий на модуль (находка 64).

    Первый случай портит имя вакансии, второй обязан увидеть исходное. Модульная фикстура
    раздавала одни и те же объекты 40+ тестам: «поправил карточку, чтобы проверить» молча
    меняло вход соседям, и поломка зависела от порядка запуска."""
    assert feed_globals["vacancies_by_id"]["v1"]["name"] == "Python Backend"
    feed_globals["vacancies_by_id"]["v1"]["name"] = "ИСПОРЧЕНО СОСЕДОМ"


@pytest.mark.parametrize("name", [
    "VACANCIES", "SAL_MAX", "SAVED_MARKS", "FX_RATES", "FX_ALIAS",
    "STATE_LABELS_PY", "RESUME_CORE_PY", "MARK_VALUES_PY",
    "CHAT_FROZEN_PY",
    # формат работы: подписи и «что считается удалёнкой» — из домена (Schedule), 08.08.2026.
    # RESUME_EXPS_PY здесь БОЛЬШЕ НЕТ: мост был мёртвым (жёсткий фильтр по грейду убран
    # 07.08, читателя в JS не осталось), а его вычисление роняло сборку на опечатке в
    # resume_profile.json::exp_ids — см. test_feed_survives_unknown_exp_id_in_profile.
    "SCHED_LABELS_PY", "REMOTE_LIKE_PY",
    # PORTAL_SITES_PY добавлен 01.08.2026 и в этот список НЕ попал — страж отставал от
    # кода почти неделю (найдено аудитом 07.08). Он и есть мост подписей порталов в JS,
    # без которого talanto и getmatch подписывались как «hh.ru».
    "PORTAL_SITES_PY",
    # наборы состояний отклика — раньше JS решал сам через startsWith('DISCARD'),
    # и это расходилось с Python по DISCARD_BY_APPLICANT (аудит 07.08.2026)
    "DISCARD_STATES_PY", "INVITED_STATES_PY",
    # шаблоны писем ленты из resume_profile.json (08.08.2026): форк должен править письма
    # профилем, а не src/feed/cover.js. Пустой список -> cover.js берёт свои дефолты.
    "FEED_COVER_TEMPLATES_PY",
])
def test_feed_data_defines_expected_global(name, feed_globals):
    """Каждый глобал, от которого зависит JS-лента, обязан присутствовать в бандле.

    ПРИСУТСТВИЕ — не весь договор: мост, инжектящий `{}` вместо словаря, этот тест
    оставлял бы зелёным (аудит 09.08.2026). ЗНАЧЕНИЕ каждого моста проверяется отдельными
    тестами литералами ниже (`test_state_labels_bridge_*`, `test_portal_sites_bridge_*`,
    `test_response_state_sets_bridge_*`, `test_schedule_bridge_*`, `CHAT_FROZEN_PY`,
    `MARK_VALUES_PY`, `FEED_COVER_TEMPLATES_PY`, `SAL_MAX`, `FX_*`). Presence-only остаётся
    у `RESUME_CORE_PY`: он читается из `resume_profile.json` запускающего, и литерал
    проверял бы чужое резюме, а не договор проекта (та же причина, по которой его нет
    в `test_feed_bridge.py`)."""
    assert f"const {name} = " in feed_globals["text"]


def test_globals_guard_covers_every_injected_const(feed_globals):
    """Список выше не должен отставать от `feed.py::build_feed`.

    Именно это и произошло с `PORTAL_SITES_PY`: глобал появился в коде, а страж его не знал,
    и «каждый глобал обязан присутствовать» проверялось для десяти из одиннадцати. Считаем
    фактические `const` в бандле и сверяем с числом случаев параметризации."""
    import re
    actual = set(re.findall(r"^const ([A-Z_]+) = ", feed_globals["text"], re.M))
    guarded = set(
        test_feed_data_defines_expected_global.pytestmark[0].args[1]  # type: ignore[attr-defined]
    )
    assert actual == guarded, f"не под стражем: {sorted(actual - guarded)}"


def test_feed_cover_templates_injected_from_profile(tmp_path):
    """Письма ленты правятся `resume_profile.json`, а не `src/feed/cover.js`: форк с другим
    резюме не должен трогать исходник (08.08.2026). В JSON нельзя положить функцию, поэтому
    едут СТРОКИ с подстановками, а оборачивает их `cover.js::coverTemplates()`."""
    js = _build_feed_data(tmp_path, FEED_COVER_TEMPLATES=["Здравствуйте! Вакансия {role}{company}."])
    assert _const(js, "FEED_COVER_TEMPLATES_PY") == ["Здравствуйте! Вакансия {role}{company}."]


def test_feed_cover_templates_empty_without_profile(tmp_path):
    # ключа в профиле нет -> пустой список -> cover.js берёт свои дефолты, поведение прежнее.
    # Значение задаём явно: иначе тест читал бы профиль запускающего и падал бы у того,
    # кто свои шаблоны как раз настроил.
    js = _build_feed_data(tmp_path, FEED_COVER_TEMPLATES=[])
    assert _const(js, "FEED_COVER_TEMPLATES_PY") == []


def test_frozen_chat_kinds_injected_from_python(feed_globals):
    # тупиковые виды чата — единый источник в chat_class.FROZEN_KINDS; хардкод 'ack' в JS
    # разъезжался бы с Python (в ленте он был в трёх местах). Ожидаемое — литерал, а не
    # list(FROZEN_CODES): иначе тест повторил бы реализацию и прошёл при любой её ошибке.
    assert _const(feed_globals["text"], "CHAT_FROZEN_PY") == ["ack", "bot_interview"]


@pytest.mark.parametrize("vid, field, expected", [
    ("v1", "name", "Python Backend"),
    ("v1", "url", "https://hh.ru/vacancy/v1"),
    ("v1", "employer", "Acme"),
    ("v1", "city", "Москва"),
    ("v1", "sal_from", 200_000),
    ("v1", "sal_mid", 200_000),
    ("v1", "currency", "RUR"),
    ("v1", "exp", "1–3 года"),          # Experience.label (EXP_LABELS['between1And3']) — ПОКАЗ
    ("v1", "exp_id", "between1And3"),   # Experience.hh_id — КОД для чипов и скоринга resume.js
    ("v1", "schedule", "remote"),
    ("v1", "techs", ["Python", "Docker"]),
    ("v1", "remote_any", True),         # schedule=remote
    ("v1", "role", "Backend"),
    ("v1", "resp", 7),
    ("v1", "status", "INVITATION"),     # из store.statuses
    ("v1", "needs_form", False),
    ("v1", "form_dead", True),          # свип: status=empty -> не актуальна
    ("v1", "source", "hh"),
    ("v2", "sal_mid", 3_000),
    ("v2", "currency", "USD"),
    ("v2", "exp", "3–6 лет"),
    ("v2", "exp_id", "between3And6"),
    ("v2", "schedule", "fullDay"),
    ("v2", "remote_any", False),        # office + текст без маркеров удалёнки
    ("v2", "role", "Аналитик"),
    ("v2", "resp", None),
    ("v2", "status", None),             # нет чата -> статуса нет
    ("v2", "needs_form", True),         # v2 в store.forms
    ("v2", "form_dead", False),         # кеш ok -> живая
    ("v2", "source", "hirify"),
    # ts последнего ЖИВОГО ответа HR: ботовый (10.07) и наш (03.07) не считаются
    ("v1", "hr_ts", "2026-07-04T11:00:00+03:00"),
    ("v2", "hr_ts", ""),                # переписки нет
])
def test_vacancy_card_field_survives_roundtrip(vid, field, expected, feed_globals):
    assert feed_globals["vacancies_by_id"][vid][field] == expected


@pytest.mark.parametrize("vid", ["v1", "v2"])
def test_hr_reply_ts_is_empty_without_chats(vid, tmp_path):
    """Без переписки поле пустое у ВСЕХ карточек.

    РЕГРЕССИЯ 08.08.2026 (находка 59, тот же класс, что funnel-инцидент того же дня):
    `feed.py::_last_hr_replies` зовёт `store.chat_messages()`, а фикстура сборки его не
    подменяла — `hr_ts` считался из CRM-состояния запускающего, и результат теста зависел
    от того, кто ответил разработчику в чатах HH. Растяжка `_live_crm_tripwire` держит
    подмену на месте, этот тест — что подменённое пусто именно пусто."""
    cards = {v["id"]: v for v in _const(_build_feed_data(tmp_path), "VACANCIES")}
    assert cards[vid]["hr_ts"] == ""


def test_mark_values_bridge_matches_python(feed_globals):
    # словарь пометок в JS = инжект из marks.py (иначе разъезжается: инцидент "discard")
    # литерал, а не list(MARK_VALUES): иначе тест повторяет реализацию и пройдёт при её ошибке
    assert _const(feed_globals["text"], "MARK_VALUES_PY") == ["applied", "rejected"]


@pytest.mark.parametrize("field", ["id", "age", "gap", "fresh"])
def test_freshness_and_id_fields_present_in_card(field, feed_globals):
    # Свежесть (age/gap/fresh) и id — структурные поля карточки, лента их читает всегда.
    assert field in feed_globals["vacancies_by_id"]["v1"]


def test_sal_max_is_computed_in_rubles(feed_globals):
    """SAL_MAX — верх ползунка В РУБЛЯХ, а не «максимум как пришло» (регрессия 08.08.2026).

    В выдаче v1 = 200 000 RUR и v2 = 3 000 USD. При курсе USD=1.0 / RUB=100.0 это 200 000
    и 300 000 рублей, значит верх шкалы 300 000. Сырой максимум дал бы 200 000: сравнивались
    числа разных валют, из-за чего одна узбекская вилка задирала шкалу до 35 млн, а
    `main.js::rescaleSalary` конвертировал получившееся число ещё раз, уже как рубли."""
    assert feed_globals["sal_max"] == 300_000


def test_sal_max_ignores_single_outlier(tmp_path):
    """Верх шкалы — 99-й перцентиль, поэтому одиночный выброс её не задирает.

    Сто вакансий по 100 000 ₽ и одна на 35 000 000 ₽: перцентиль (nearest-rank, округление
    вверх до шага 10 000) даёт 100 000. По максимуму рабочий диапазон занимал бы 0.3% длины
    ползунка, и фильтр по зарплате был бы неуправляем."""
    records = [
        _record(f"n{i}", name="Backend", city="Москва", employer="Acme", source="hh",
                mid=100_000, currency="RUR", exp="between1And3", schedule="remote",
                techs=["Python"], role=Role.BACKEND, url="https://hh.ru/vacancy/1")
        for i in range(100)
    ]
    records.append(
        _record("rich", name="Backend", city="Москва", employer="Acme", source="hh",
                mid=35_000_000, currency="RUR", exp="between1And3", schedule="remote",
                techs=["Python"], role=Role.BACKEND, url="https://hh.ru/vacancy/2"))
    assert _const(_build_feed_data(tmp_path, records=records), "SAL_MAX") == 100_000


def test_sal_max_trims_the_top_tenth_of_the_range(tmp_path):
    """Верх шкалы отрезает верхнюю десятину вилок (`SALARY_PCTL = 90`).

    Сто вилок 10 000, 20 000 ... 1 000 000 ₽. Девяностый перцентиль по nearest-rank —
    девяностая по возрастанию, то есть 900 000; шаг 10 000 её уже кратен, округлять нечего.
    Проверяем именно СЕМАНТИКУ обрезки хвоста, а не значение константы: до 08.08.2026 брался
    максимум (дал бы 1 000 000), а p99 отдал бы 990 000 — рабочий диапазон тонул в хвосте
    глобальной удалёнки при медиане по срезу 314 947 ₽."""
    records = [
        _record(f"n{i}", name="Backend", city="Москва", employer="Acme", source="hh",
                mid=10_000 * i, currency="RUR", exp="between1And3", schedule="remote",
                techs=["Python"], role=Role.BACKEND, url="https://hh.ru/vacancy/1")
        for i in range(1, 101)
    ]
    assert _const(_build_feed_data(tmp_path, records=records), "SAL_MAX") == 900_000


def test_sal_max_without_any_salary_falls_back_to_default(tmp_path):
    # Вилок нет ни у одной карточки -> шкала не схлопывается в ноль, а берёт дефолтный верх.
    records = [_record("free", name="Backend", city="Москва", employer="Acme", source="hh",
                       mid=None, currency="", exp="between1And3", schedule="remote",
                       techs=["Python"], role=Role.BACKEND, url="https://hh.ru/vacancy/3")]
    assert _const(_build_feed_data(tmp_path, records=records), "SAL_MAX") == 500_000


def test_schedule_bridge_carries_domain_labels_and_remote_like(feed_globals):
    """Подписи формата и «что считается удалёнкой» едут из домена (08.08.2026).

    Ожидаемое — литералы из `domain/schedule.py`, а не `{s.hh_code: s.label ...}`: иначе
    тест повторил бы реализацию и прошёл при любой её ошибке. Раньше моста не было вовсе,
    и `model.js::SCHED_LABELS` совпадал с доменом ровно в одном коде из трёх."""
    assert _const(feed_globals["text"], "SCHED_LABELS_PY") == {
        "remote": "Удалённо", "flexible": "Гибрид", "fullDay": "Офис",
    }
    assert _const(feed_globals["text"], "REMOTE_LIKE_PY") == ["remote", "flexible"]


def test_state_labels_bridge_carries_every_status_label(feed_globals):
    """Подписи статусов отклика инжектятся ЦЕЛИКОМ и дословно (`chat.py::STATE_LABELS`).

    Ожидаемое — литералы из спеки (`docs/chat.md`), а не `chat.STATE_LABELS`: сверка с
    источником проверяла бы обратимость json.dumps, а не подписи. Инцидент, ради которого
    мост завели, — расхождение подписей («Звонок» вместо «Телефон-интервью», «Закрыта»
    вместо «Вакансия закрыта»); второй конец того же моста (офлайн-фолбэк в `model.js`)
    стережёт `test_feed_bridge.py`. Пустой словарь в инжекте гасил бы подписи бейджей
    у всех карточек, а страж присутствия этого не видел (аудит 09.08.2026).

    Отдельно про два «Отказа»: `DISCARD` и `DISCARD_BY_EMPLOYER` подписаны одинаково,
    а `DISCARD_BY_APPLICANT` — «Вы отказались», это НАШ отказ, не работодателя."""
    assert _const(feed_globals["text"], "STATE_LABELS_PY") == {
        "RESPONSE":               "Отклик",
        "INVITATION":             "Приглашение",
        "CONSIDER":               "Рассматривается",
        "PHONE_INTERVIEW":        "Телефон-интервью",
        "INTERVIEW":              "Интервью",
        "ASSESSMENT":             "Тестовое",
        "HIRED":                  "Оффер",
        "DISCARD":                "Отказ",
        "DISCARD_BY_EMPLOYER":    "Отказ",
        "DISCARD_BY_APPLICANT":   "Вы отказались",
        "DISCARD_VACANCY_CLOSED": "Вакансия закрыта",
    }


def test_portal_sites_bridge_carries_the_domain_of_every_source(feed_globals):
    """Домены порталов едут из `config.PORTAL_SITES` — по одному на каждый источник.

    ИНЦИДЕНТ 01.08.2026: в JS стоял тернарник на два портала, и talanto с getmatch
    подписывались как «hh.ru». Литералы, а не сверка с `PORTAL_SITES`: инжект пустого
    словаря вернул бы ленту к «имя источника как есть» молча — presence-страж это
    пропускал (аудит 09.08.2026)."""
    assert _const(feed_globals["text"], "PORTAL_SITES_PY") == {
        "hh":        "hh.ru",
        "hirify":    "hirify.me",
        "talanto":   "talanto.work",
        "getmatch":  "getmatch.ru",
        "arbeitnow": "arbeitnow.com",
        "himalayas": "himalayas.app",
        "web3":      "web3.career",
        "themuse":   "themuse.com",
        "jobicy":    "jobicy.com",
    }


@pytest.mark.parametrize("name, expected", [
    # НАШ отказ (DISCARD_BY_APPLICANT) в набор НЕ входит: JS раньше решал сам через
    # startsWith('DISCARD') и красил такую карточку «Отказ», а воронка относила её
    # в «без исхода» (аудит 07.08.2026).
    ("DISCARD_STATES_PY", ["DISCARD", "DISCARD_BY_EMPLOYER", "DISCARD_VACANCY_CLOSED"]),
    # CONSIDER («Рассматривается») входит намеренно — это «позитив/в работе», а не
    # приглашение; набор един с воронкой (docs/chat.md).
    ("INVITED_STATES_PY", ["ASSESSMENT", "CONSIDER", "HIRED", "INTERVIEW",
                           "INVITATION", "PHONE_INTERVIEW"]),
])
def test_response_state_sets_bridge_carries_python_membership(name, expected, feed_globals):
    """Наборы состояний отклика инжектятся списками кодов; сравниваем СОСТАВ, литералами.

    Порядок в договор не входит (в JS это `includes`), поэтому сортируем обе стороны —
    в отличие от `MARK_VALUES_PY`, где порядок задаёт порядок кнопок и проверяется как есть.
    До 09.08.2026 у обоих наборов проверялось только присутствие `const`."""
    assert sorted(_const(feed_globals["text"], name)) == expected


def test_exp_chips_are_rendered_from_exp_labels(tmp_path):
    """Чипы «Опыт» рендерятся из `config.EXP_LABELS`, а не четырьмя литералами в шаблоне.

    Подписи грейдов жили в трёх местах сразу (EXP_LABELS -> feed.html.j2 -> resume.js), и
    правка EXP_LABELS молча ломала и чипы, и скоринг (аудит 08.08.2026). Подменяем словарь
    подписей: чип обязан приехать с новой подписью и с КОДОМ в value — по коду фильтр
    сравнивает `exp_id` карточки, поэтому переименование подписи выдачу не меняет."""
    _, html = _build_feed_files(tmp_path, EXP_LABELS={"noExperience": "Стажёр",
                                                      "moreThan6": "Сеньор"})
    assert '<input type="checkbox" class="lang-cb exp-cb" id="exp-0" value="noExperience">' in html
    assert '<label class="lang-label" for="exp-0">Стажёр</label>' in html
    assert '<input type="checkbox" class="lang-cb exp-cb" id="exp-1" value="moreThan6">' in html
    assert '<label class="lang-label" for="exp-1">Сеньор</label>' in html
    assert "1–3 года" not in html          # старых литералов в шаблоне не осталось


def test_dead_resume_exps_bridge_is_gone(feed_globals):
    """`RESUME_EXPS_PY` больше не эмитится (08.08.2026).

    Мост был мёртвым: жёсткий фильтр по грейду убран 07.08, читателя в JS не осталось
    (`grep '_PY' src/`), зато его вычисление `[EXP_LABELS[e] for e in RESUME_EXP_IDS]`
    роняло `hh.py feed`/`serve` с KeyError на опечатке в `resume_profile.json::exp_ids`
    («1-3 года» с дефисом вместо en-dash). Сборка больше не читает профиль ради подписей."""
    assert "RESUME_EXPS_PY" not in feed_globals["text"]


def test_saved_marks_pass_through_unchanged(feed_globals):
    assert feed_globals["saved_marks"] == _MARKS


def test_fx_rates_pass_through_and_alias_canonizes_currency_codes(feed_globals):
    """FX_RATES — ровно инжектированные курсы; FX_ALIAS — канонизация кодов валют.

    Алиасы сравниваются с ЛИТЕРАЛОМ (`docs/domain.md`: RUR->RUB, BYR->BYN, USDT->USD),
    а не с `rates.CURRENCY_ALIAS`: сборщик сериализует ровно этот объект, и сверка с ним
    проверяла бы обратимость json.dumps. Правка вида `RUR -> RUR` прошла бы обе стороны
    одинаково, а лента перестала бы находить курс для рублёвых вилок (аудит 09.08.2026).
    Тот же литерал уже стоит в `tests/feed/model.test.js::resolveCur`."""
    assert feed_globals["fx_rates"] == _FX
    assert feed_globals["fx_alias"] == {"RUR": "RUB", "BYR": "BYN", "USDT": "USD"}


# ─── Чип «Не указан» (инцидент 10.08.2026) ──────────────────────────────────────
# Фильтр Python + arbeitnow + опыт давал НОЛЬ вакансий даже при всех нажатых кнопках,
# а без фильтра по опыту — 538. Пустой `exp_id` не совпадал ни с одним из четырёх кодов,
# и «выбрано всё» переставало быть равносильно «фильтр снят». Задето 27 880 карточек:
# web3 и arbeitnow поголовно, talanto — 22 625, getmatch — 482.
def test_experience_chips_include_the_unknown_grade(feed_globals):
    """Пятый чип с ПУСТЫМ кодом — иначе карточки без грейда недостижимы фильтром."""
    from hrwork.presentation.views import feed as F
    assert F.EXP_UNKNOWN == ("", "Не указан")


def test_chip_code_matches_the_empty_exp_id_of_a_gradeless_card(tmp_path):
    """Код чипа обязан совпадать с тем, что лежит в карточке, СИМВОЛ В СИМВОЛ:
    фильтр сравнивает их равенством, и «None» вместо «''» тихо вернул бы тот же ноль."""
    from hrwork.presentation.views import feed as F
    records = [_record("w3", name="Backend Developer", city="Remote", employer="DAO",
                       source="web3", mid=None, currency="", exp=None, schedule="remote",
                       techs=["Python"], role=Role.BACKEND, url="https://web3.career/1")]
    card = _const(_build_feed_data(tmp_path, records=records), "VACANCIES")[0]
    assert card["exp_id"] == F.EXP_UNKNOWN[0]
