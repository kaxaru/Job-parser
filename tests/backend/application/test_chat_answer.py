"""Тесты шаблонных ответов боту. Главное свойство: движок НЕ ВЫДУМЫВАЕТ фактов."""
import pytest

from hrwork.application.apply.chat.chat_answer import Grade, VacancyContext, suggest

PROF = {"answers": {
    "stack": ["Python", "FastAPI", "PostgreSQL", "Docker"],
    "years_text": "Около 1 года коммерческого опыта.",
    "format_text": "Ищу удалённый формат; офис рассматриваю в Москве и Самаре.",
    "answer_negative": True,
}}
EMPTY = {"answers": {}}


# ── деньги и место: без контекста вакансии / без фактов в профиле — молчим ──
def test_salary_silent_without_vacancy_context():
    # PROF без salary_by_grade и ctx=None: назвать вилку не из чего
    assert suggest("Укажите ваши зарплатные ожидания", PROF) is None
    assert suggest("Рассматриваете вилку до 100 на руки?", PROF) is None


def test_place_silent_without_office_city():
    assert suggest("Готовы на фултайм в офис в Алматы?", PROF) is None


def test_bargaining_always_human():
    # торг вокруг названной суммы — не факт о себе, а переговоры (_NEVER)
    prof = {"answers": {"salary_by_grade": {"junior": "90 000 — 100 000"}}}
    ctx = VacancyContext(name="Junior Python", experience="noExperience")
    assert suggest("Готовы ли снизить ожидания по зарплате?", prof, ctx) is None


# ── опыт с технологией ──
def test_has_experience_yes_only_for_known_stack():
    # Текст ЦЕЛИКОМ, как у английского близнеца ниже: вхождение прошло бы и на ответе
    # с лишними фактами («Да, есть опыт: FastAPI, Kubernetes.») — то есть ровно
    # на выдуманном факте, против которого написан весь файл (аудит 09.08.2026).
    a = suggest("Есть ли у вас опыт работы с FastAPI?", PROF)
    assert a["rule"] == "has_exp_yes"
    assert a["text"] == "Да, есть опыт: FastAPI."


def test_has_experience_no_for_unknown_tech():
    a = suggest("Есть ли опыт работы с OHIF Viewer?", PROF)
    assert a["rule"] == "has_exp_no"


def test_never_claims_unknown_tech():
    # Ключевое: технология не из стека не подтверждается — движок МОЛЧИТ, и вопрос уходит
    # человеку. Дизъюнкция `a is None or "A/B" not in a["text"]` пропускала главный дефект
    # файла (БАГ 20.07): ответ «Да, есть опыт: Python.» на вопрос про A/B-тесты подстроки
    # «A/B» не содержит и оставлял тест зелёным (аудит 09.08.2026).
    assert suggest("Занимались ли вы дизайном A/B-тестов?", PROF) is None


def test_specific_practice_over_known_tech_is_silent():
    # БАГ 20.07: «опыт автоматизации тестирования ML-сервисов НА Python» -> движок отвечал
    # «Да, есть опыт: Python», подтверждая не то, о чём спросили. Теперь молчим.
    assert suggest("Был ли у вас опыт автоматизации тестирования МЛ-сервисов на Python?",
                   PROF) is None
    assert suggest("Есть ли опыт построения ETL-пайплайнов на Python?", PROF) is None
    # а чистый вопрос про технологию по-прежнему отвечается
    assert suggest("Есть ли опыт работы с Docker?", PROF)["rule"] == "has_exp_yes"


def test_past_stack_without_particle_li():
    # БАГ 20.07: «У вас есть опыт с C#?» -> молчали, т.к. regex требовал «есть ЛИ»
    prof = {"answers": {"stack": ["Python"], "stack_past": ["C#", ".NET"]}}
    a = suggest("У вас есть опыт с C#?", prof)
    assert a["rule"] == "has_exp_past"
    assert "C#" in a["text"]


def test_negative_answers_disabled_by_default():
    prof = {"answers": {"stack": ["Python"]}}          # answer_negative не задан
    assert suggest("Есть ли опыт с Kubernetes?", prof) is None


# ── пустой профиль: молчим, вопрос уходит человеку ──
@pytest.mark.parametrize("question", [
    "Есть ли опыт с Python?",
    "Сколько лет опыта?",
    "С какими фреймворками работали?",
    "Какой формат работы?",
])
def test_no_facts_no_answer(question):
    assert suggest(question, EMPTY) is None


# ── прочее ──
def test_stack_enumeration():
    a = suggest("С какими фреймворками вы работали?", PROF)
    assert a["rule"] == "stack_list"
    assert a["text"] == "Работал с: Python, FastAPI, PostgreSQL, Docker."


def test_years_and_format_from_profile():
    assert suggest("Сколько лет вы работали разработчиком?", PROF)["rule"] == "years"
    assert suggest("Какой формат работы вам подходит?", PROF)["rule"] == "format"


def test_confirmation():
    assert suggest("Используем эти ответы?", PROF)["rule"] == "confirm"


def test_unknown_question_returns_none():
    assert suggest("Расскажите о вашем самом сложном проекте", PROF) is None


# ══════════════════ английская ветка ══════════════════
# Профиль с переводами части фактов: english_text_en есть, education_text_en НЕТ —
# так проверяется, что при отсутствии перевода движок молчит, а не отвечает по-русски.
PROF_EN = {"answers": {
    "stack": ["Python", "FastAPI", "PostgreSQL", "Docker"],
    "stack_past": ["C#", ".NET"],
    "years_text": "Около 5 лет.",
    "years_text_en": "About 5 years in total.",
    "english_text": "Английский — B1.",
    "english_text_en": "English — B1 (intermediate).",
    "education_text": "Высшее: матфак, 2011.",
    "answer_negative": True,
}}


def test_lang_detected_by_script():
    assert suggest("Do you have experience with Docker?", PROF_EN)["lang"] == "en"
    assert suggest("Есть ли опыт работы с Docker?", PROF_EN)["lang"] == "ru"


def test_english_question_gets_english_answer():
    a = suggest("Do you have experience with Docker?", PROF_EN)
    assert a["rule"] == "has_exp_yes"
    assert a["text"] == "Yes, I have experience with Docker."


def test_english_negative_answer_localized():
    a = suggest("Have you worked with Kubernetes?", PROF_EN)
    assert a["rule"] == "has_exp_no"
    assert a["text"] == "No, I haven't worked with that."


def test_english_fact_taken_from_en_key():
    assert suggest("How many years of experience do you have?", PROF_EN)["text"] \
        == "About 5 years in total."
    assert suggest("What is your English level?", PROF_EN)["text"] == "English — B1 (intermediate)."


def test_no_translation_means_silence_not_russian():
    # education_text есть, education_text_en НЕТ -> молчим. Ответить по-русски на
    # английский вопрос — та же ложь, что и выдумать факт.
    assert suggest("What is your education?", PROF_EN) is None
    assert suggest("Какое у вас образование?", PROF_EN)["rule"] == "education"


@pytest.mark.parametrize("question", [
    "What is your expected salary?",
    "What is your day rate?",
    "How much do you expect to earn?",
    "Are you willing to relocate to Berlin?",
    "Would you be willing to move to Warsaw?",
])
def test_money_and_relocation_blocked_in_english(question):
    assert suggest(question, PROF_EN) is None


@pytest.mark.parametrize("question", [
    "Какой у вас ожидаемый уровень заработной платы на этой должности?",
    "Вы готовы работать в г. Шатура?",
])
def test_ru_money_and_place_holes_closed(question):
    # БАГ 20.07, найден на живых чатах: «заработной платы» не покрывалось паттерном
    # «зарплат», «готовы работать в г. X» — паттерном «переезд». Оба уходили в автоответ.
    assert suggest(question, PROF_EN) is None


def test_english_stack_enumeration():
    a = suggest("What frameworks have you worked with?", PROF_EN)
    assert a["rule"] == "stack_list"
    assert a["text"].startswith("I've worked with:")


def test_english_specific_practice_still_silent():
    # «C# И Windows Forms»: C# в прошлом стеке, Windows Forms — нет. Подтвердить C#
    # значило бы ответить не на тот вопрос. Живой вопрос из чата 20.07.
    assert suggest(
        "Do you have hands-on experience developing applications with C# and Windows Forms?",
        PROF_EN) is None


# ══════════════════ зарплата по грейду и место работы ══════════════════
SAL = {"answers": {
    "salary_by_grade": {"junior": "90 000 — 100 000", "middle": "150 000 — 170 000",
                        "senior": "200 000 — 220 000"},
    "office_city": "Приволжск", "office_city_en": "Privolzhsk",
}}


@pytest.mark.parametrize("ctx,expected", [
    (VacancyContext(name="Junior Python Developer", experience="between3And6"), Grade.JUNIOR),
    (VacancyContext(name="Старший разработчик", experience="between1And3"), Grade.SENIOR),
    (VacancyContext(name="Python-разработчик", experience="between1And3"), Grade.JUNIOR),
    (VacancyContext(name="Python-разработчик", experience="between3And6"), Grade.MIDDLE),
    (VacancyContext(name="Python-разработчик", experience="moreThan6"), Grade.SENIOR),
    (VacancyContext(name="Python-разработчик", experience=""), None),
    (VacancyContext(name="Python-разработчик", experience="weirdCode"), None),
])
def test_grade_title_beats_experience_field(ctx, expected):
    # тайтл сильнее поля опыта; 1-3 года = JUNIOR (решение владельца профиля 20.07);
    # неизвестный код опыта -> None (мягкий парсер), а не средняя вилка наугад
    assert ctx.grade is expected


def test_salary_answered_by_vacancy_grade():
    # БАГ 20.07: «ожидаемый уровень заработной платы» уходил в автоответ мимо фильтра;
    # теперь на него отвечает вилка по грейду вакансии
    q = "Какой у вас ожидаемый уровень заработной платы на этой должности?"
    jun = VacancyContext(name="Стажёр-разработчик", experience="noExperience")
    sen = VacancyContext(name="Senior Python Developer", experience="moreThan6")
    assert suggest(q, SAL, jun)["text"] == "Ожидаемый уровень — 90 000 — 100 000."
    assert suggest(q, SAL, sen)["text"] == "Ожидаемый уровень — 200 000 — 220 000."


def test_salary_silent_when_grade_unknown():
    # контекст есть, но грейд не определить -> человеку, а не средняя вилка наугад
    assert suggest("Укажите зарплатные ожидания", SAL,
                   VacancyContext(name="Разработчик", experience="")) is None


def test_place_remote_for_other_city():
    # БАГ 20.07: «готовы работать в г. Шатура?» уходил мимо фильтра; теперь — честный
    # ответ «удалённо, офис только в Приволжске»
    a = suggest("Здравствуйте, Вы готовы работать в г. Шатура ?", SAL,
                VacancyContext(name="Инженер", experience="between1And3", city="Шатура"))
    assert a["rule"] == "place_remote"
    assert "Приволжск" in a["text"]


def test_place_own_city_offers_office():
    a = suggest("Готовы работать в г. Приволжск?", SAL,
                VacancyContext(name="Инженер", experience="between1And3", city="Приволжск"))
    assert a["rule"] == "place_own_city"
    assert a["text"] == "Готов работать удалённо или в офисе в Приволжск."


def test_salary_and_place_english():
    q_ctx = VacancyContext(name="Middle Python Developer", experience="between3And6")
    assert suggest("What is your expected salary?", SAL, q_ctx)["text"]         == "My expectation is 150 000 — 170 000."
    a = suggest("Are you willing to relocate to Berlin?", SAL,
                VacancyContext(name="Dev", experience="between1And3", city="Berlin"))
    assert a["rule"] == "place_remote"
    assert "Privolzhsk" in a["text"]


def test_polite_preamble_does_not_block_simple_question():
    # БАГ 20.07 (найден тестом chat_reply): «Спасибо за отклик. Есть ли опыт с Docker?»
    # -> _residual оставлял «спасибо отклик» -> движок молчал на простом вопросе.
    # Вежливая обвязка добавлена в _BOILERPLATE.
    a = suggest("Здравствуйте, Антон! Спасибо за отклик. Есть ли у вас опыт работы с Docker?",
                PROF)
    assert a["rule"] == "has_exp_yes"
    assert "Docker" in a["text"]


def test_polite_preamble_does_not_weaken_residual_guard():
    # при этом конкретная практика поверх технологии по-прежнему уходит человеку
    assert suggest("Спасибо за отклик! Есть ли опыт настройки мониторинга кластеров "
                   "на Docker?", PROF) is None


def test_bot_interviewer_preamble_stripped():
    # ИИ-помощник HH ведёт цепочку: «Понял, спасибо за ответ. Следующий вопрос: …».
    # Эта обвязка не должна прятать сам вопрос (найдено на живом чате 20.07).
    prof = {"answers": {"stack": ["Jira", "Confluence", "Docker"]}}
    a = suggest("Понял, спасибо за подробный ответ. Следующий вопрос: работали ли "
                "вы с Jira или Confluence в проектах?", prof)
    assert a["rule"] == "has_exp_yes"
    assert "Jira" in a["text"]


def test_bot_preamble_does_not_weaken_residual_guard():
    # но конкретная практика поверх технологии по-прежнему уходит человеку
    prof = {"answers": {"stack": ["Docker"], "answer_negative": True}}
    assert suggest("Спасибо! Есть ли опыт настройки распределённого трейсинга "
                   "на Docker в продакшене?", prof) is None


def test_years_with_specific_tech_is_silent():
    # БАГ 21.07: «сколько лет вы работаете с React?» -> движок слал ОБЩИЙ стаж
    # (на .NET/1С), бот переспрашивал, loop крутил 5 отправок. React-стажа нет -> молчим.
    prof = {"answers": {
        "stack": ["Python", "React", "JavaScript"], "stack_past": ["C#"],
        "years_text": "Общий опыт 5 лет: EPAM (.NET).",
        "years_python_text": "Python — 3 года.",
    }}
    assert suggest("Сколько лет вы работаете с React в коммерческих проектах?", prof) is None
    assert suggest("сколько лет опыта с C#?", prof) is None
    # но общий и Python-стаж по-прежнему отвечаются
    assert suggest("Сколько лет вы в разработке?", prof)["rule"] == "years"
    assert suggest("Сколько лет опыта на Python?", prof)["text"] == "Python — 3 года."


# ═══════ авто-«нет»: «нет» — тоже утверждение о кандидате (аудит 08.08.2026) ═══════
# Три РАЗНЫХ случая, и путать их нельзя:
#   а) технология в профиле ЕСТЬ, но названа иначе -> «нет» это прямая ложь -> молчим;
#   б) технологию узнали и в профиле её точно нет -> честное «нет» допустимо;
#   в) вопрос вообще не про технологию (практика, процесс) -> молчим, тема человеку.
PROF_NEG = {"answers": {
    "stack": ["PostgreSQL", "Docker", "REST API"],
    "stack_past": ["C#", ".NET"],
    "answer_negative": True,
}}


@pytest.mark.parametrize("question", [
    "Есть ли опыт с Postgres?",
    "Есть ли опыт работы с постгресом?",
    "Работали с Докером?",
    "Приходилось работать с докер-компоуз?",
    "Есть ли опыт с SQL?",
    "Do you have experience with Postgres?",
])
def test_no_negative_about_tech_that_profile_has_under_another_name(question):
    # НАХОДКА 08.08.2026 (прогон агента): stack=[PostgreSQL, Docker] -> «Есть ли опыт
    # с Postgres?» -> «Нет, с этим не работал.» Движок соврал про собственный стек, потому
    # что сверял точную подстроку и не знал ни транслита, ни кириллических словоформ.
    assert suggest(question, PROF_NEG) is None


@pytest.mark.parametrize("question, expected", [
    ("Есть ли опыт работы с OHIF Viewer?", "Нет, с этим не работал."),
    ("Работали с Kubernetes?", "Нет, с этим не работал."),
    ("Приходилось работать с Кубером?", "Нет, с этим не работал."),
    ("Есть ли опыт с Redis?", "Нет, с этим не работал."),
    ("Have you worked with Kubernetes?", "No, I haven't worked with that."),
])
def test_negative_stays_for_technology_absent_from_profile(question, expected):
    # Обратная сторона фикса: узнаваемая ЧУЖАЯ технология по-прежнему получает честное
    # «нет». Отдельно про Kubernetes: стек с «.NET» не должен его глушить — «.NET»
    # нормализуется в «net», а «kubernetes» содержит «net» внутри слова.
    a = suggest(question, PROF_NEG)
    assert a["rule"] == "has_exp_no"
    assert a["text"] == expected


@pytest.mark.parametrize("question", [
    "Был ли опыт нагрузочного тестирования?",
    "Есть ли опыт наставничества?",
    "Занимались ли вы код-ревью?",
    "Есть ли опыт постановки процессов?",
    "Do you have experience with load testing?",
    "Do you have experience in code review?",
])
def test_silence_when_question_is_not_about_a_technology(question):
    # НАХОДКА 08.08.2026: «Был ли опыт нагрузочного тестирования?» -> «Нет, с этим
    # не работал.» Факта нет вообще: практика — не технология, и стек её не опровергает.
    assert suggest(question, PROF_NEG) is None


def test_profile_synonyms_block_a_negative_about_own_tech():
    # Словарь синонимов расширяется из профиля словами, а не регексом: у владельца
    # Greenplum, бот пишет «GPDB» — «нет» про свой же стек уходить не должно.
    prof = {"answers": {"stack": ["Greenplum"], "answer_negative": True,
                        "tech_synonyms": {"Greenplum": ["gpdb", "гринплам"]}}}
    assert suggest("Есть ли опыт с GPDB?", prof) is None
    assert suggest("Работали с Гринпламом?", prof) is None


def test_profile_synonyms_make_a_cyrillic_name_recognizable_as_tech():
    # Вторая сторона того же ключа: слово из словаря опознаётся как НАЗВАНИЕ технологии,
    # поэтому про отсутствующий в стеке Гринплам можно честно ответить «нет».
    prof = {"answers": {"stack": ["Docker"], "answer_negative": True,
                        "tech_synonyms": {"Greenplum": ["гринплам"]}}}
    a = suggest("Работали с Гринпламом?", prof)
    assert a["rule"] == "has_exp_no"
    assert a["text"] == "Нет, с этим не работал."


def test_broken_profile_synonyms_do_not_break_the_run():
    # Профиль пишет человек: строка вместо списка — опечатка. Дефолтный словарь остаётся
    # в силе (Docker в стеке -> «нет» про Докер не уходит), прогон не падает.
    prof = {"answers": {"stack": ["Docker"], "answer_negative": True,
                        "tech_synonyms": {"Greenplum": "гринплам", 42: None}}}
    assert suggest("Работали с Докером?", prof) is None


def test_years_with_cyrillic_tech_name_is_silent():
    # БАГ 21.07 в кириллическом написании: «Сколько лет работаете с Докером?» проходил мимо
    # латинского backstop'а и получал ОБЩИЙ стаж, набранный на другом стеке.
    prof = {"answers": {
        "stack": ["Python", "Docker"], "stack_past": ["C#"],
        "years_text": "Общий опыт 5 лет: EPAM (.NET).",
        "years_python_text": "Python — 3 года.",
    }}
    assert suggest("Сколько лет вы работаете с Докером?", prof) is None
    assert suggest("Сколько лет вы в разработке?", prof)["text"] == "Общий опыт 5 лет: EPAM (.NET)."


# ══════════════ intent-роутер: маршрутизация по метке LLM ══════════════
from hrwork.application.apply.chat.chat_intent import IntentResult  # noqa: E402

PROF_FE = {"answers": {
    "stack": ["Python", "FastAPI", "React", "TypeScript", "JavaScript"],
    "stack_past": ["C#"],
    "years_text": "Общий опыт 5 лет 9 мес: EPAM (.NET), Коралл (1С).",
    "years_python_text": "Python — 3 года.",
    "years_frontend_text": "Около 2 лет фронтенда: React, TypeScript.",
    "answer_negative": True,
}}


def _i(label, *tech):
    return IntentResult(label, tuple(tech))


def test_intent_years_tech_frontend_routes_to_frontend():
    a = suggest("сколько лет с этим?", PROF_FE, intent=_i("years_tech", "React"))
    assert a is not None
    assert a["rule"] == "frontend"


def test_intent_years_tech_python_routes_to_python():
    a = suggest("а сколько именно?", PROF_FE, intent=_i("years_tech", "FastAPI"))
    assert a is not None
    assert a["text"] == "Python — 3 года."


def test_intent_years_tech_unknown_is_silent():
    # Kafka не фронт и не python -> выделенного стажа нет -> человеку (не общий стаж!)
    assert suggest("сколько лет?", PROF_FE, intent=_i("years_tech", "Kafka")) is None


def test_intent_years_general_answers_general():
    a = suggest("а сколько всего?", PROF_FE, intent=_i("years"))
    assert a["rule"] == "years"
    assert "5 лет" in a["text"]


def test_intent_years_backstop_silences_tech_in_question():
    # LLM ошиблась: метка years, но в вопросе латинская технология -> backstop -> молчим
    assert suggest("сколько лет с Kafka?", PROF_FE, intent=_i("years")) is None


def test_intent_depth_frontend_answers_not_silent():
    # «какой опыт с React» (depth) -> фронт-факт есть -> отвечаем (не теряем как regex-путь)
    a = suggest("какой у вас опыт?", PROF_FE, intent=_i("depth", "React"))
    assert a is not None
    assert a["rule"] == "frontend"


def test_intent_depth_no_tech_is_human():
    assert suggest("расскажите о сложном проекте", PROF_FE, intent=_i("depth")) is None


def test_intent_has_exp_yes():
    a = suggest("работали?", PROF_FE, intent=_i("has_exp", "FastAPI"))
    assert a["rule"] == "has_exp_yes"
    assert "FastAPI" in a["text"]


def test_intent_has_exp_no_lie_about_tech_present_under_another_name():
    # Тот же инвариант на LLM-маршруте: метка has_exp + tech=[Postgres] при PostgreSQL
    # в стеке. «Нет» отсюда уйти не должно; утвердительный ответ по синониму движок
    # тоже не собирает (синонимы только ЗАПРЕЩАЮТ «нет»), поэтому вопрос — человеку.
    assert suggest("работали?", PROF_NEG, intent=_i("has_exp", "Postgres")) is None


@pytest.mark.parametrize("label", ["has_exp", "years", "years_tech", "depth"])
def test_intent_never_widens_answered(label):
    # пустой профиль: любой intent-маршрут -> None (intent не создаёт факт из воздуха)
    empty = {"answers": {}}
    assert suggest("вопрос?", empty, intent=_i(label, "React")) is None


def test_intent_non_cluster_falls_through_to_regex():
    # метка salary не в кластере -> intent игнорируется, идёт regex-путь (тут молчит без ctx)
    assert suggest("Укажите зарплатные ожидания", PROF_FE, intent=_i("salary")) is None


def test_never_guard_before_intent():
    # торг всегда человеку, даже с intent-меткой
    assert suggest("готовы снизить ожидания?", PROF_FE, intent=_i("years")) is None


def test_intent_depth_no_tech_ignores_question_words():
    # БАГ 21.07 (dry-run): depth без tech, но в длинном вопросе случайное «backend» ->
    # ложный python-стаж. Маршрут ТОЛЬКО по intent.tech, не по тексту -> молчим.
    q = "данная вакансия не подразумевает постоянной занятости на backend, вы готовы?"
    assert suggest(q, PROF_FE, intent=_i("depth")) is None


def test_intent_years_tech_ignores_question_words():
    # years_tech с пустым tech -> молчим, даже если в вопросе есть py/front слово
    assert suggest("что по формату python-команды?", PROF_FE, intent=_i("years_tech")) is None
