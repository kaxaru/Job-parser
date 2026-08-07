"""Шаблонные ответы на вопросы бот-рекрутеров. Два языка: русский и английский.

ПРИНЦИП: движок НИЧЕГО НЕ ВЫДУМЫВАЕТ. Отвечает только тем, что явно указано в
`resume_profile.json` -> блок `answers`. Нет факта — нет ответа (возвращаем None), вопрос
уходит человеку. Приписать опыт, которого нет, хуже, чем промолчать: это репутация
кандидата перед работодателем.

ЯЗЫК: определяется по вопросу (кириллица -> ru, иначе en) и держится сквозным.
Факты берутся с суффиксом языка: для английского вопроса нужен `years_text_en` и т.п.
Нет перевода факта — молчим, а НЕ отвечаем по-русски на английский вопрос: это тот же
принцип «нет факта — нет ответа», просто применённый к языку.

Категории собраны по живым бот-диалогам (19-20.07): боты спрашивают ПОСЛЕДОВАТЕЛЬНО,
по одному вопросу, и первым часто идёт вопрос про деньги — он всегда уходит человеку.
"""
import re
from dataclasses import dataclass
from typing import Any

from hrwork.application.apply.chat.chat_class import PLACE_Q, SALARY_Q
from hrwork.config import BASE_DIR
from hrwork.domain.grade import Grade
from hrwork.infrastructure.storage import read_json_or

PROFILE_FILE = BASE_DIR / "resume_profile.json"

RU, EN = "ru", "en"
_CYRILLIC = re.compile(r"[а-яё]", re.I)
# Сколько слов остатка ещё похоже на НАЗВАНИЕ технологии, а не на описание практики.
# 2 — эмпирика по живым чатам: «Kubernetes», «OHIF Viewer», «Apache Spark» проходят;
# «функциональное или регрессионное тестирование» — нет.
_MAX_TECH_TOKENS = 2


def detect_lang(question: str) -> str:
    """Язык вопроса. Кириллица -> ru; иначе en. Смешанный текст («опыт с C#») —
    русский: латиница в русском вопросе это названия технологий, не смена языка."""
    return RU if _CYRILLIC.search(question or "") else EN


# Вопрос «есть ли опыт с X» — вытаскиваем X и сверяем со стеком из профиля.
# RU: «ли» НЕ обязательно — боты пишут и «У вас есть опыт с C#?», и «Есть опыт с Docker?».
# EN: «do you have…», «have you worked…», «are you familiar with…».
_HAS_EXP = re.compile(
    r"(?:есть|был[аo]?|имеется|занимались|работали|работал|приходилось|владеете|знаком"
    r"|do you have|have you|are you familiar|hands.?on|worked with|experience (?:with|in))"
    r"[^?]{0,140}", re.I)

# Шаблонная обвязка вопроса: убираем её и упомянутые технологии — что осталось, то и есть
# СУТЬ вопроса. Если остаток непустой, спрашивают про конкретную практику («автоматизация
# тестирования ML-сервисов на Python»), а не про технологию — подтверждать нельзя.
_BOILERPLATE = re.compile(
    r"\b(?:есть|ли|у|вас|вы|ваш\w*|был[аои]?|было|имеется|опыт\w*|работ\w*|с|со|в|во|на|и|или"
    r"|использов\w*|владеете|приходилось|занимались|знаком\w*|коммерческ\w*|практическ\w*"
    r"|пожалуйста|расскажите|уточните|скажите|какой|какие|каким|каких|а|же|уже|ещё|еще"
    # вежливая обвязка письма: не может быть сутью технического вопроса, но без неё
    # «Спасибо за отклик. Есть ли опыт с Docker?» давало остаток «спасибо отклик»
    # и движок молчал на простом вопросе (найдено тестом 20.07)
    r"|здравствуйте|доброе|добрый|утро|день|вечер|привет|антон|спасибо|благодар\w*"
    r"|отклик\w*|интерес\w*|ваканси\w*|компани\w*|резюме|кандидат\w*|позици\w*"
    r"|меня|зовут|нас|наш\w*|мы|нам|это|очень|рады"
    # реплики бота-интервьюера между вопросами: «Понял, спасибо за подробный ответ.
    # Следующий вопрос: …», «…в проектах, над которыми работали». Тоже обвязка —
    # без неё цепочка вопросов от ИИ-помощника HH вся уходила в молчание (20.07)
    r"|понял|понятно|отлично|хорошо|подробн\w*|ответ\w*|следующ\w*|вопрос\w*"
    r"|проект\w*|котор\w*|\bнад\b|также|ещ[её]"
    # английская обвязка
    r"|you|your|have|has|had|any|the|and|or|with|for|about|please|could|would|tell|know"
    r"|experience|experienced|familiar|worked|working|work|hands|hello|hi|dear|thanks"
    r"|commercial|professional|production)\b", re.I)
_PUNCT = re.compile(r"[?!.,:;()\[\]«»\"'’—–\-/]+")
# ОТКРЫТЫЙ вопрос «с чем работали» — перечисляем стек. Домен между «с какими» и предметом
# должен быть ПУСТ: «с какими ML-фреймворками» — узкий вопрос, и перечисление всего стека
# в ответ на него ввело бы в заблуждение (дефект найден dry-run 20.07: на «С каким
# из ML-фреймворков?» движок вываливал 23 технологии, ни одна из которых не ML).
_WHICH_STACK = re.compile(
    r"с как(?:им|ими)\s+(?:из\s+)?(?:фреймворк|язык|технолог|инструмент|стек|бд|базам)"
    r"|what\s+(?:framework|language|technolog|tool|stack|database)", re.I)
_YEARS = re.compile(
    r"сколько лет|стаж|как долго|сколько времени|опыт\w*\s+работы\s+в\s+год"
    r"|how many years|how long have|years of experience", re.I)
# Вопрос про стаж ИМЕННО на Python/backend — отвечаем питоновским стажем, а не общим.
_PY_CONTEXT = re.compile(r"python|питон|бэкенд|backend|fastapi|django|flask", re.I)
# Фронтенд — зонтик: любой из этих маркеров -> отвечаем ГРУППОВЫМ фронт-стажем (~2 года),
# а не молчим и не выдумываем стаж по отдельной технологии. Redux НЕ включён намеренно
# (опыт минимальный — прямой вопрос про него уходит человеку).
_FRONT_CONTEXT = re.compile(
    r"\breact\b|\bvue\b|\bangular\b|\btypescript\b|\bts\b|\bjavascript\b|\bjs\b"
    r"|фронт|frontend|front.?end|в[её]рстк|\bhtml\b|\bcss\b|\bscss\b|\bsass\b"
    r"|webpack|vite|\bspa\b|\bui\b(?!.?ux)"
    r"|front.?end developer|клиентск\w*\s+част", re.I)
_FORMAT = re.compile(
    r"удал[её]н|офис|гибрид|формат работы|график работы"
    r"|remote|on.?site|hybrid|work format|work schedule|relocation-free", re.I)
_CONFIRM = re.compile(
    r"используем эти ответы|всё верно|верно\?|подтвержда"
    r"|is that correct|are these correct|please confirm", re.I)
_ENGLISH = re.compile(r"английск|english|уровень\s+язык|language level", re.I)
_EDUCATION = re.compile(
    r"образован|вуз|университет|диплом|специальност\w*\s+по\s+диплому"
    r"|education|university|degree|diploma|major", re.I)
_CITIZEN = re.compile(
    r"гражданств|разрешени\w*\s+на\s+работу|патент|вид на жительство"
    r"|citizenship|work permit|work visa|right to work|residence", re.I)
# Темы «деньги»/«место» — ЕДИНЫЙ источник в chat_class (SALARY_Q/PLACE_Q): те же регэкспы
# зажигают бейдж «решай сам» в ленте. Дыры чинятся в одном месте, а не в двух.
_SALARY_Q = SALARY_Q
_PLACE_Q = PLACE_Q

# Торг вокруг уже названной суммы — всегда человеку: это переговоры, а не факт о себе.
_NEVER = re.compile(
    r"готовы (?:ли )?(?:снизить|рассмотреть меньше|подвинуться)|ваш минимум|минимальн\w*\s+сумм"
    r"|торг|ниже рынка"
    r"|lower (?:your )?expectation|negotiable|come down on", re.I)


@dataclass(frozen=True)
class VacancyContext:
    """Контекст вакансии, из которой пришёл вопрос. Нужен, чтобы назвать вилку по грейду
    и понять, свой ли это город. Пустой контекст -> движок молчит про деньги."""
    name: str = ""
    experience: str = ""      # HH-код: noExperience | between1And3 | between3And6 | moreThan6
    city: str = ""

    @property
    def grade(self) -> Grade | None:
        return Grade.from_vacancy(self.name, self.experience)

# Готовые формулировки ответов на каждом языке. Факты сюда не попадают — только обвязка.
_PHRASES = {
    RU: {
        "confirm": "Да, всё верно.",
        "has_exp_yes": "Да, есть опыт: {techs}.",
        "has_exp_past": "Да, работал ранее с {past}, сейчас основной стек — {stack}.",
        "has_exp_no": "Нет, с этим не работал.",
        "stack_list": "Работал с: {stack}.",
        "salary": "Ожидаемый уровень — {range}.",
        "place_remote": "Работаю удалённо. Офис рассматриваю только в {city}.",
        "place_own_city": "Готов работать удалённо или в офисе в {city}.",
    },
    EN: {
        "confirm": "Yes, that's correct.",
        "has_exp_yes": "Yes, I have experience with {techs}.",
        "has_exp_past": "Yes, I worked with {past} earlier; my current main stack is {stack}.",
        "has_exp_no": "No, I haven't worked with that.",
        "stack_list": "I've worked with: {stack}.",
        "salary": "My expectation is {range}.",
        "place_remote": "I work remotely. On-site is an option only in {city}.",
        "place_own_city": "I can work remotely or on-site in {city}.",
    },
}


def load_profile() -> dict[str, Any]:
    return read_json_or(PROFILE_FILE, {}) or {}


def _answers(profile: dict[str, Any]) -> dict[str, Any]:
    return (profile or {}).get("answers") or {}


def _fact(ans: dict[str, Any], key: str, lang: str) -> str | None:
    """Факт из профиля НА ЯЗЫКЕ ВОПРОСА. Для en ищем `<key>_en`; нет перевода -> None
    (молчим), а не отдаём русский текст в ответ на английский вопрос."""
    val = ans.get(key) if lang == RU else ans.get(f"{key}_en")
    return val if isinstance(val, str) and val.strip() else None


def _known_stack(profile: dict[str, Any]) -> list[str]:
    """Технологии, про которые МОЖНО сказать «есть опыт». Пусто -> движок молчит."""
    return [str(s) for s in (_answers(profile).get("stack") or []) if str(s).strip()]


def _past_stack(profile: dict[str, Any]) -> list[str]:
    """Технологии из ПРОШЛОГО опыта: отвечаем честной оговоркой «работал ранее»,
    а не наравне с текущим стеком (в резюме они помечены как прошлый опыт)."""
    return [str(s) for s in (_answers(profile).get("stack_past") or []) if str(s).strip()]


def _practice_answer(question: str, ans: dict[str, Any], lang: str) -> tuple[str, str] | None:
    """Ответ про ПРАКТИКУ (тестирование, ETL, мониторинг…), а не про технологию.

    `answers.practices` — список {"q": regex тем, "a": ответ, "a_en": перевод}, порядок
    значим (первое совпадение). Нужен потому, что «проводили ли вы регрессионное
    тестирование?» — вопрос о процессе: технологии в нём нет, и без факта движок либо
    молчал, либо выдавал рискованное «нет, не работал» (dry-run 20.07).
    Кривой regex в профиле пропускаем, а не роняем прогон.
    """
    for i, p in enumerate(ans.get("practices") or []):
        if not isinstance(p, dict):
            continue
        try:
            if not re.search(p.get("q") or r"(?!)", question, re.I):
                continue
        except re.error:
            continue
        text = p.get("a") if lang == RU else p.get("a_en")
        if isinstance(text, str) and text.strip():
            return text, f"practice_{p.get('id') or i}"
    return None


def _mentioned_tech(question: str, stack: list[str]) -> list[str]:
    """Какие технологии из нашего стека упомянуты в вопросе."""
    q = question.lower()
    return [s for s in stack if s.lower() in q]


def _residual(question: str, techs: list[str]) -> str:
    """Что останется от вопроса, если убрать шаблонную обвязку и названные технологии.
    Непустой остаток = спрашивают про конкретную ПРАКТИКУ, а не про владение технологией."""
    q = question.lower()
    for t in sorted(techs, key=len, reverse=True):
        q = q.replace(t.lower(), " ")
    q = _BOILERPLATE.sub(" ", q)
    q = _PUNCT.sub(" ", q)
    return " ".join(w for w in q.split() if len(w) > 2)


# ── Факт-хелперы кластера {has_exp, years, years_tech, depth} ──
# Вынесены, чтобы regex-путь suggest И intent-роутер звали ОДИН код (нет расхождения).
# Возвращают {"text","rule","lang"} либо None. `out`-логику дублируем как _mk (модульный
# уровень — замыкание suggest сюда не дотянуть).
def _mk(text: str, rule: str, lang: str) -> dict[str, Any]:
    return {"text": text, "rule": rule, "lang": lang}


def _answer_frontend(ans: dict[str, Any], lang: str) -> dict[str, Any] | None:
    fe = _fact(ans, "years_frontend_text", lang)
    return _mk(fe, "frontend", lang) if fe else None


def _answer_years_python(ans: dict[str, Any], lang: str) -> dict[str, Any] | None:
    val = _fact(ans, "years_python_text", lang) or _fact(ans, "years_text", lang)
    return _mk(val, "years", lang) if val else None


def _answer_years(q: str, ans: dict[str, Any], stack: list[str], past: list[str], lang: str) -> dict[str, Any] | None:
    """Стаж. Python-контекст -> питоновский; конкретная технология -> молчим (backstop);
    иначе общий. Backstop (латинский тех-токен / упомянут стек-тех) — ВТОРОЙ рубеж, держит
    молчание даже если LLM ошиблась меткой years на вопросе про чужую технологию."""
    if _PY_CONTEXT.search(q):
        return _answer_years_python(ans, lang)
    if _mentioned_tech(q, stack + past) or (lang == RU and re.search(r"[A-Za-z]{3,}", q)):
        return None
    val = _fact(ans, "years_text", lang)
    return _mk(val, "years", lang) if val else None


def _answer_has_exp(q: str, ans: dict[str, Any], stack: list[str], past: list[str], lang: str) -> dict[str, Any] | None:
    say = _PHRASES[lang]
    if not stack:
        return None
    hit = _mentioned_tech(q, stack)
    pst = _mentioned_tech(q, past)
    if hit or pst:
        if _residual(q, hit + pst):               # конкретная практика поверх технологии -> человек
            return None
        if hit:
            return _mk(say["has_exp_yes"].format(techs=", ".join(hit)), "has_exp_yes", lang)
        return _mk(say["has_exp_past"].format(past=", ".join(pst), stack=", ".join(stack[:3])),
                   "has_exp_past", lang)
    rest = _residual(q, [])
    if ans.get("answer_negative") and 0 < len(rest.split()) <= _MAX_TECH_TOKENS:
        return _mk(say["has_exp_no"], "has_exp_no", lang)
    return None


# Кластер меток, которыми управляет intent-роутер; прочие метки -> regex-путь без изменений.
_INTENT_CLUSTER = frozenset({"has_exp", "years", "years_tech", "depth"})


def _route_cluster(intent: Any, q: str, ans: dict[str, Any], stack: list[str], past: list[str],
                   lang: str) -> dict[str, Any] | None:
    """Маршрутизация проблемного кластера по метке LLM к ЛОКАЛЬНОМУ факту.
    Инвариант: нет выделенного факта -> None (человек), не выдумываем."""
    label = intent.label
    if label == "has_exp":
        # технология из intent.tech может быть НЕ в тексте вопроса («работали?» + tech=[FastAPI])
        # -> добавляем её к вопросу, чтобы _mentioned_tech нашёл её в стеке.
        q_aug = (q + " " + " ".join(intent.tech)).strip() if intent.tech else q
        return _answer_has_exp(q_aug, ans, stack, past, lang)
    if label == "years":
        return _answer_years(q, ans, stack, past, lang)      # backstop внутри
    if label in ("years_tech", "depth"):
        # Стаж/глубина по КОНКРЕТНОЙ технологии — маршрут ТОЛЬКО по intent.tech (что LLM
        # явно выделила), НЕ по всему вопросу: случайное «backend» в длинном тексте иначе
        # давало ложный python-стаж на вопрос про формат занятости (найдено dry-run 21.07).
        # Нет tech -> нет выделенного факта -> молчим (открытый вопрос человеку).
        tech = " ".join(intent.tech)
        if not tech.strip():
            return None
        if _FRONT_CONTEXT.search(tech):
            return _answer_frontend(ans, lang)
        if _PY_CONTEXT.search(tech):
            return _answer_years_python(ans, lang)
        return None
    return None


def suggest(question: str, profile: dict[str, Any] | None = None,
            ctx: VacancyContext | None = None,
            intent: Any = None) -> dict[str, Any] | None:
    """Предложить ответ на вопрос бота (ru или en — по языку вопроса).

    ctx — контекст вакансии (тайтл/опыт/город): без него вопросы про деньги и место
    уходят человеку, с ним отвечаем вилкой по грейду и форматом по городу.

    -> {"text": …, "rule": …, "lang": …} либо None, если факта нет / отвечать должен человек.
    Вызывающий обязан показать текст ПЕРЕД отправкой (или отправлять только по флагу).
    """
    q = " ".join((question or "").split())
    if not q:
        return None
    prof = profile if profile is not None else load_profile()
    ans = _answers(prof)
    stack = _known_stack(prof)
    lang = detect_lang(q)
    say = _PHRASES[lang]

    def out(text: str, rule: str) -> dict[str, Any]:
        return {"text": text, "rule": rule, "lang": lang}

    if _NEVER.search(q):
        return None                                   # торг — только человек (ПЕРЕД intent)

    # LLM-роутер: если намерение из проблемного кластера (years/years_tech/depth/has_exp) —
    # маршрутизируем по метке к тем же факт-хелперам. Прочие метки / intent=None -> regex ниже.
    # intent НИКОГДА не расширяет отвечаемое: нет факта -> None. См. docs/chat.md.
    if intent is not None and intent.label in _INTENT_CLUSTER:
        return _route_cluster(intent, q, ans, stack, _past_stack(prof), lang)

    # Деньги: вилка по грейду вакансии. Нет контекста / грейд неизвестен / вилка
    # не задана -> человеку.
    if _SALARY_Q.search(q):
        grade = ctx.grade if ctx else None
        if grade is None:
            return None
        rng = (ans.get("salary_by_grade") or {}).get(grade.code)
        if not (isinstance(rng, str) and rng.strip()):
            return None
        return out(say["salary"].format(range=rng), f"salary_{grade.code}")

    # Место работы: если вакансия в нашем офисном городе — «удалённо или офис в X»,
    # иначе — «удалённо; офис только в X». Город профиля не задан -> человеку.
    if _PLACE_Q.search(q):
        own = _fact(ans, "office_city", lang)
        if not own:
            return None
        vac_city = (ctx.city if ctx else "") or ""
        same = vac_city.strip().lower() == (ans.get("office_city") or "").strip().lower()
        key = "place_own_city" if same else "place_remote"
        return out(say[key].format(city=own), key)

    if _CONFIRM.search(q):
        return out(say["confirm"], "confirm")

    # Фронтенд — ГРУППОЙ, до _HAS_EXP/_YEARS: любой вопрос про фронт-технологию или фронт
    # вообще («сколько лет с React», «какой опыт с TypeScript», «делали вёрстку?») -> один
    # честный ответ про ~2 года фронт-опыта. Зонтик покрывает хвост формулировок, который
    # regex по каждой технологии не осилит. Нет факта в профиле -> обычные ветки ниже.
    if _FRONT_CONTEXT.search(q) and (r := _answer_frontend(ans, lang)):
        return r

    # Стаж: про PYTHON/backend, про КОНКРЕТНУЮ технологию и про «вообще» — разные ответы.
    # Логика в _answer_years (общая с intent-роутером); NB: regex различение years vs
    # years_tech принципиально хрупко — это класс, ради которого и делался LLM-классификатор.
    if _YEARS.search(q):
        return _answer_years(q, ans, stack, _past_stack(prof), lang)

    # Простые фактические справки: отвечаем ровно тем, что записано в профиле.
    for rx, key, rule in ((_ENGLISH, "english_text", "english"),
                          (_EDUCATION, "education_text", "education"),
                          (_CITIZEN, "citizenship_text", "citizenship")):
        if rx.search(q):
            val = _fact(ans, key, lang)
            return out(val, rule) if val else None

    if _FORMAT.search(q):
        fmt = _fact(ans, "format_text", lang)
        return out(fmt, "format") if fmt else None

    if _WHICH_STACK.search(q):
        if not stack:
            return None
        return out(say["stack_list"].format(stack=", ".join(stack)), "stack_list")

    # Практики (тестирование, ETL, мониторинг…) — раньше _HAS_EXP, но только если
    # в вопросе НЕ названа наша технология: про неё точнее ответит has_exp_yes.
    if not _mentioned_tech(q, stack):
        pr = _practice_answer(q, ans, lang)
        if pr:
            return out(pr[0], pr[1])

    if _HAS_EXP.search(q):
        return _answer_has_exp(q, ans, stack, _past_stack(prof), lang)

    return None
