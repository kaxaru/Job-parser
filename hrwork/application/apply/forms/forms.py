"""Авто-заполнение форм-опросников HH (часть отклика, RFC-003 + авто-submit).

Форма — это ЧАСТЬ отклика: заполнение живёт inline в `autoclick.apply_one` (когда отклик
упирается в анкету и включён FORMS_LLM). Здесь — переиспользуемое ядро `try_autofill(page, cand)`:
резолвит ВСЕ поля (словарь/факты/LLM); при полноте заполняет + пишет письмо + ЖМЁТ «Откликнуться»;
хоть один пробел -> НЕ шлёт, лог пробелов -> в форм-очередь (человек пополняет form_answers).

ПОЛНОТА — это ТРИ этапа, и каждый гейтит отправку: съём страницы (`form_read.extract_form`,
`missed == 0`), резолв ответа на каждое поле, фактическое заполнение (`_fill_field`). Хромает
любой — анкета уходит человеку.

`run()` — тем же путём обрабатывает НАКОПЛЕННУЮ форм-очередь (`store.forms()`): бэклог, что
осел до inline или где пробелы уже закрыты словарём. `--dry` — только показать резолвинг.
Дренаж — такой же реальный отклик, поэтому он и УВАЖАЕТ суточную квоту, и инкрементит её.

БЕЗОПАСНОСТЬ (docs/security.md): у LLM нет execution-канала (только строка -> .fill/.check/
.select_option/print) — машина не тронута ни при какой инъекции. Ответы только из фактов/словаря;
radio/checkbox строго из опций (membership); свободный текст проходит денилист
`form_fill._denied`. origin только hh.ru.
"""
from __future__ import annotations

import contextlib
import random
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from hrwork.application.apply import cover
from hrwork.application.apply.candidates import OutOfScope, out_of_scope
from hrwork.application.apply.chat import chat_answer
from hrwork.application.apply.forms import form_fill, form_read
from hrwork.application.apply.forms.form_status import FormSweepStatus
from hrwork.application.apply.outcome import SETTLED_MARKS, ApplyChannel
from hrwork.application.apply.runtime.store import store
from hrwork.config import FORMS_ENABLED, body, log

_SUBMIT = '[data-qa="vacancy-response-submit-popup"]'          # «Откликнуться» (с ответами теста)
_LETTER = 'textarea[data-qa="vacancy-response-popup-form-letter-input"]'
_LETTER_TOGGLE = '[data-qa="vacancy-response-letter-toggle"]'  # «Сопроводительное письмо / Добавить»

# Пауза между ОТПРАВЛЕННЫМИ откликами дренажа, сек. Своя, а не `APPLY_PAUSE` (4-9с): цикл по
# анкете сам занимает ~20с, и добавка в пять секунд ничего не меняла — 28.07 сплошной дренаж
# упёрся в капчу HH после 21 отклика ночью и после 8 утром. Здесь темп важнее скорости: очередь
# дренируется фоном, а капча стоит ручного вмешательства и простоя всех прогонов.
FORM_PAUSE = (25.0, 55.0)


def _is_hh(url: str) -> bool:
    """origin-гард: только https://…hh.ru. Чужой origin не открываем в сессионном браузере."""
    with contextlib.suppress(Exception):
        u = urlparse(url or "")
        host = (u.hostname or "").lower()
        return u.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))
    return False


# ленивый кеш {id: (employer, desc, name, salary_floor, experience_code)}
_VAC_CTX: dict[str, Any] | None = None


def _load_vac_ctx() -> dict[str, Any]:
    global _VAC_CTX
    if _VAC_CTX is None:
        _VAC_CTX = {}
        with contextlib.suppress(Exception):
            from hrwork.infrastructure.storage import vacancy_repository
            for r in vacancy_repository().load():
                sal = getattr(r.vacancy, "salary", None)
                floor = None                       # пол вакансии (net RUB) для правила «если выше»
                if sal and getattr(sal, "frm", None) and getattr(sal, "currency", None) in (None, "RUR", "RUB"):
                    floor = sal.frm
                exp = getattr(r.vacancy, "experience", None)   # вилка опыта -> фолбэк грейда
                _VAC_CTX[r.vacancy.id] = (r.vacancy.employer or "",
                                          getattr(r, "requirement", "") or "", r.vacancy.name or "",
                                          floor, exp.hh_id if exp else None)
    return _VAC_CTX


def _vacancy_ctx(vid: str) -> tuple[str, str, str]:
    v = _load_vac_ctx().get(str(vid))
    return v[:3] if v else ("", "", "")


def _vacancy_floor(vid: str) -> int | None:
    v = _load_vac_ctx().get(str(vid))
    return v[3] if v else None


def _vacancy_exp(vid: str) -> str | None:
    """Код вилки опыта вакансии (`Experience.hh_id`) — тот же фолбэк грейда, что у чатов:
    без него «Python-разработчик» с вилкой 1–3 года получал в анкете MIDDLE-ставку, а
    в переписке JUNIOR-вилку (см. `form_fill.detect_grade`)."""
    v = _load_vac_ctx().get(str(vid))
    return v[4] if v else None


def _resolve(field: Any, resume_ctx: str, sal_target: int | None = None,
             vacancy_text: str = "") -> tuple[str | None, str | None]:
    """Ответ на поле. Зарплата -> ТОЛЬКО код (в LLM не уходит, приватность). Иначе: словарь ->
    suggest(text) -> LLM -> (None,None). -> (подпись/текст, own|None)."""
    if form_fill.is_salary_q(field.prompt):        # зарплата — детерминированно, НЕ провайдеру
        if not sal_target:
            return None, None
        if field.options:                          # варианты-диапазоны -> выбрать опцию
            lbl = form_fill.salary_option(field.options, sal_target)
            return (lbl, None) if lbl else (None, None)
        return f"от {sal_target:,} руб.".replace(",", " "), None   # свободный ввод -> «от N руб.»
    m = form_fill.match_answer(field.prompt, field.options)
    if m:
        return m
    # «чем интересна ваша компания» — единственный класс, где ответа нет ни в резюме, ни в
    # словаре: он про КОНКРЕТНУЮ вакансию, поэтому отвечаем по её описанию
    if form_fill.is_motivation_q(field.prompt) and not field.options:
        mot = form_fill.answer_motivation(field.prompt, vacancy_text, resume_ctx)
        if mot:
            return mot, None
    if field.ftype in (form_read.FieldType.TEXT, form_read.FieldType.TEXTAREA):
        ans = chat_answer.suggest(field.prompt)
        if ans and ans.get("text"):
            return ans["text"], None
    llm = form_fill.answer_field(field.prompt, field.ftype, field.options, resume_ctx)
    if llm:
        return llm, None
    # знание-квиз (QA/фреймворки) — экспертный выбор. Пустой ctx = гейт FORMS_LLM выключен
    # (см. _dry_preview): answer_quiz контекст не проверяет и пошёл бы в провайдера мимо гейта
    if field.options and resume_ctx:
        quiz = form_fill.answer_quiz(field.prompt, field.options, resume_ctx)
        if quiz:
            return quiz, None
    return None, None


def _fill_field(page: Any, field: Any, value: str, own: str | None = None) -> bool:
    """Заполнить ОДНО поле (DOM-литерал; никогда submit). «Свой вариант» -> парный textarea."""
    with contextlib.suppress(Exception):
        if field.ftype is form_read.FieldType.SELECT:
            page.locator(field.selector).first.select_option(label=value)
        elif field.ftype in (form_read.FieldType.RADIO, form_read.FieldType.CHECKBOX):
            v = value                                          # value = выбранная ПОДПИСЬ -> её value
            if field.opt_values and value in field.options:
                v = field.opt_values[field.options.index(value)]
            page.locator(f'{field.selector}[value="{v}"]').first.check()
            if own and v == "open":                            # «Свой вариант» -> свободный текст
                page.locator(f'textarea[name="{field.name}_text"]').first.fill(own)
        else:
            page.locator(field.selector).first.fill(value)
        return True
    return False


def _fill_cover(page: Any, vid: str, rec: dict[str, Any], cover_mode: str) -> None:
    """Сопроводительное — в поле письма на самой форме (не через chatik). Textarea спрятан за
    тоглом «Добавить» (`_LETTER_TOGGLE`, выверено живым прогоном 134804227) — сперва раскрыть."""
    emp, desc, nm = _vacancy_ctx(vid)
    cand = SimpleNamespace(id=str(vid), name=nm or rec.get("name", ""), employer=emp, desc=desc)
    # build_cover — НАША логика, и она ВНЕ suppress: под общим гасителем её падение
    # (AttributeError в шаблоне, битый профиль) выглядело как «поле письма не найдено», то
    # есть свой дефект маскировался под проблему HH. Fail fast на своей ошибке: она
    # повторится на КАЖДОЙ вакансии, и упасть на первой дешевле, чем разослать 60 откликов
    # без письма. Гасим только DOM-вызовы ниже.
    text = cover.build_cover(cand, cover_mode)
    with contextlib.suppress(Exception):
        if not page.locator(_LETTER).count():
            page.locator(_LETTER_TOGGLE).first.click(timeout=3_000)
            page.wait_for_timeout(500)
        page.locator(_LETTER).first.fill(text)
        log.info("[{}] сопроводительное вписано в поле формы", vid)
        return
    log.warning("[{}] письмо НЕ вписано (поле не найдено/fill упал) — отклик уйдёт без него", vid)


def _submitted(page: Any) -> bool:
    """Отклик реально ушёл: появился чат-топик или «Вы откликнулись» (иначе не метим applied)."""
    with contextlib.suppress(Exception):
        return bool(page.locator('[data-qa="vacancy-response-link-view-topic"]').count()
                    or page.get_by_text("Вы откликнулись").count())
    return False


def _wait_submitted(page: Any, tries: int = 6) -> bool:
    """Поллинг подтверждения до ~12с: HH перерисовывает карточку с лагом (инцидент 134804227,
    2026-07-22 — отклик УШЁЛ, но за 2с подтверждение не успело, прогон счёл его неотправленным)."""
    for _ in range(tries):
        page.wait_for_timeout(2_000)
        if _submitted(page):
            return True
    return False


def try_autofill(page: Any, cand: Any, cover_mode: str = "template") -> bool:
    """Inline авто-отклик на анкету (вызывается из apply_one под гейтом FORMS_ENABLED, страница
    уже на форме). Резолвит ВСЕ поля; при полноте — заполняет, пишет письмо, ЖМЁТ «Откликнуться»
    -> True (верификацию делает apply_one). Пустое извлечение, НЕПОЛНЫЙ съём страницы или ХОТЬ
    ОДИН пробел -> НЕ шлёт, лог -> False (вакансия уходит в форм-очередь, человек пополняет
    form_answers)."""
    resume_ctx = form_fill.build_resume_ctx()
    snapshot = form_read.extract_form(page)
    fields = list(snapshot.fields)
    if not fields:
        log.info("[{}] анкета без извлечённых полей — в очередь (руками)", cand.id)
        return False
    # Полнота СЪЁМА — первый из трёх этапов инварианта (съём -> резолв -> заполнение).
    # Без этой проверки исключение на пятом вопросе оставляло четыре поля, все четыре
    # резолвились, и гейт полноты проходил ПО ОБРЕЗАННОМУ СПИСКУ.
    if not snapshot.complete:
        log.warning("[{}] АВТО-ОТКЛИК ПРОПУЩЕН: анкета снята НЕПОЛНО — {} из {} вопросов не "
                    "извлеклось (флэки-DOM или незнакомый контрол); отправка по обрезанному "
                    "списку запрещена, вакансия остаётся в очереди",
                    cand.id, snapshot.missed, snapshot.missed + len(fields))
        return False
    _, desc, nm = _vacancy_ctx(str(cand.id))      # описание — контекст для «чем интересна вакансия»
    sal_target = form_fill.salary_target(nm or getattr(cand, "name", ""), _vacancy_floor(cand.id),
                                         _vacancy_exp(cand.id))
    resolved = [(f, *_resolve(f, resume_ctx, sal_target, desc)) for f in fields]
    gaps = [f for f, val, _ in resolved if not val]
    if gaps:
        # Текст ВОПРОСА здесь не маскируется (в отличие от превью `_dry_preview`): это
        # публичный текст работодателя и единственное, по чему владелец поймёт, какую запись
        # добавить в `form_answers`. Маскируется подставленное ЗНАЧЕНИЕ, а его тут нет.
        for f in gaps:
            log.warning("[{}] ПРОБЕЛ анкеты (нет ответа): {}", cand.id, f.prompt[:90])
        log.warning("[{}] АВТО-ОТКЛИК ПРОПУЩЕН: {}/{} полей без ответа — пополни form_answers "
                    "в resume_profile.json", cand.id, len(gaps), len(fields))
        return False
    # Полнота РЕЗОЛВА (выше) и полнота ЗАПОЛНЕНИЯ — разные вещи. `_fill_field` глушит исключения
    # Playwright и возвращает False (селектор разъехался, поле скрыто, вариант не совпал), а
    # раньше результат выбрасывался — и submit жался по форме с дырами. Инвариант «шлём ТОЛЬКО
    # при полноте» обязан покрывать оба этапа.
    # Отдельный список с СУЖЕННЫМ типом (str вместо str | None): гейт `gaps` выше уже вернул
    # False, если хоть один ответ пуст, и типы это фиксируют — `_fill_field` требует str,
    # так что пустой ответ физически не может дойти до заполнения формы.
    ready: list[tuple[form_read.FormField, str, str | None]] = [
        (f, val, own) for f, val, own in resolved if val is not None
    ]
    unfilled = [f for f, val, own in ready if not _fill_field(page, f, val, own)]
    if unfilled:
        for f in unfilled:
            log.warning("[{}] поле НЕ вписалось (селектор/DOM): {}", cand.id, f.prompt[:90])
        log.warning("[{}] АВТО-ОТКЛИК ПРОПУЩЕН: {}/{} полей не заполнилось — отклик ушёл бы "
                    "с дырами", cand.id, len(unfilled), len(fields))
        return False
    # ЧТО именно уходит работодателю — в лог до submit. Отклик необратим, а текст свободных
    # полей пишет LLM по чужому описанию вакансии: без этой строки сработавшую инъекцию
    # (и просто неудачную формулировку) нельзя увидеть постфактум ничем.
    # ЭТА строка сознательно НЕ маскируется `config.body`, в отличие от превью `_dry_preview`:
    # она — единственная посмертная улика необратимого действия, и приватность собственного
    # лога тут дешевле, чем слепота к тому, что ушло работодателю (аудит 08.08.2026, п.49).
    for f, val, own in ready:
        log.info("[{}] анкета отправляется: {} -> {}{}", cand.id, f.prompt[:60], val[:200],
                 f" | свой вариант: {own[:200]}" if own else "")
    _fill_cover(page, str(cand.id), {"name": getattr(cand, "name", "")}, cover_mode)
    with contextlib.suppress(Exception):
        page.locator(_SUBMIT).first.click(timeout=5_000)
    if not _wait_submitted(page):                  # ВЕРИФИКАЦИЯ: отклик реально ушёл, не «нажали вслепую»
        log.warning("[{}] АВТО-ОТКЛИК НЕ ПОДТВЕРЖДЁН («Вы откликнулись» не появилось за ~12с) — "
                    "остаётся в очереди; ПРОВЕРЬ КАРТОЧКУ РУКАМИ перед повтором (мог уйти с лагом)",
                    cand.id)
        return False
    log.success("[{}] АВТО-ОТКЛИК ОТПРАВЛЕН и ПОДТВЕРЖДЁН: {} полей", cand.id, len(fields))
    return True


def _open_form(page: Any) -> None:
    """С карточки вакансии перейти на форму отклика: клик «Откликнуться/пройти тест» + модалка
    релокации. Клик ОТКРЫВАЕТ форму с вопросами — это НЕ отправка (submit — отдельная _SUBMIT).
    Нужен только `run()` (бэклог хранит URL карточки); в inline-пути apply_one клик уже сделан."""
    with contextlib.suppress(Exception):
        page.locator('[data-qa="vacancy-response-link-top"]').first.click(timeout=8_000)
    with contextlib.suppress(Exception):
        page.locator('[data-qa="relocation-warning-confirm"]').first.click(timeout=3_000)
    page.wait_for_timeout(1_500)


def _dry_preview(page: Any, vid: str, rec: dict[str, Any]) -> None:
    """Показать поля + резолвинг для одной вакансии, ничего не трогая."""
    resume_ctx = form_fill.build_resume_ctx() if FORMS_ENABLED else ""
    fields = form_read.extract_fields(page)
    _, desc, _ = _vacancy_ctx(vid)
    sal_target = form_fill.salary_target(rec.get("name", ""), _vacancy_floor(vid),
                                         _vacancy_exp(vid))
    gaps = 0
    # ТЕЛА МАСКИРУЮТСЯ (`config.body`, находка аудита 08.08.2026, п.49): пара «поле анкеты ->
    # подставленное значение» уходила в `logs/*.log` дословно и на уровне INFO — вместе с
    # зарплатой, которую превью подставляет само (`form_fill.salary_target`). Служебная часть
    # видна: тип поля, id вакансии, число полей и пробелов. `keep` оставляет ровно столько,
    # чтобы отличить одно поле анкеты от другого; «DECLINE (пробел)» — наша метка, не текст.
    # Дословно — `LOG_BODIES=1`, ровно для того превью и запускают.
    for f in fields:
        val, own = _resolve(f, resume_ctx, sal_target, desc)
        gaps += not val
        tail = f" (+свой: {body(own)})" if own else ""
        log.info("  [{}] {} -> {}", f.ftype.code, body(f.prompt, keep=25),
                 (f"{body(val)}{tail}" if val else "DECLINE (пробел)"))
    log.info("[{}] {} — полей {}, пробелов {}", vid, rec.get("name"), len(fields), gaps)


def sweep(only: str = "", headless: bool = True, refresh: bool = False) -> dict[str, Any]:
    """Пройти форм-очередь и снять структуру каждой анкеты (вопрос/тип/опции) в кеш
    `forms_cache.json`. Пропускает уже закешированные (если не `refresh`) — СЛЕДУЮЩИЙ проход
    трогает только новые формы, мёртвые (0 полей) больше не открывает. DRY: submit НЕ жмётся.
    Crash-safe: каждая форма пишется в кеш сразу (обрыв не теряет собранное)."""
    queue = store.forms()
    if only:
        queue = {k: v for k, v in queue.items() if k == str(only)}
    cached = set() if refresh else store.cached_form_ids()
    todo = {k: v for k, v in queue.items() if k not in cached}
    log.info("Свип форм: очередь {}, в кеше {}, к сбору {}", len(queue), len(cached), len(todo))
    if not todo:
        return {"queue": len(queue), "swept": 0, "cached": len(cached)}

    from playwright.sync_api import sync_playwright

    from hrwork.application.apply.autoclick import (
        _goto,
        _launch,
        _logged_in,
        _page,
        _single_instance,
        is_captcha,
    )
    swept = 0
    with _single_instance(), sync_playwright() as p:
        ctx = _launch(p, headless)
        page = _page(ctx)
        with contextlib.suppress(Exception):           # кап навигации: медленная форма не стопорит свип
            page.set_default_navigation_timeout(25_000)
            page.set_default_timeout(20_000)
        if not _logged_in(page):
            log.error("Нет сессии HH — сначала: hh.py autoclick --login")
            return {"queue": len(queue), "swept": 0, "cached": len(cached)}
        for i, (vid, rec) in enumerate(todo.items(), 1):
            url = rec.get("url", "")
            fields, status = [], FormSweepStatus.OK
            if not _is_hh(url):
                status = FormSweepStatus.ERROR
            else:
                try:
                    _goto(page, url)
                    # ОБЯЗАТЕЛЬНО до извлечения: на странице капчи полей нет, и свип записал бы
                    # ЖИВУЮ анкету как EMPTY -> `--clean` вычистил бы по этому признаку всю
                    # очередь. Один неудачный момент стоил бы всего бэклога.
                    if is_captcha(page):
                        log.error("HH показал капчу (/account/captcha) — свип ОСТАНОВЛЕН на {}, "
                                  "кеш не тронут. Пройди проверку вручную и повтори", vid)
                        break
                    _open_form(page)                           # карточка -> форма (не submit)
                    # opt_values нужны и в кеше: без них `--dry` показывает превью не тем, чем
                    # оно будет (боевой путь берёт поля с ЖИВОЙ страницы, а превью — отсюда)
                    fields = [{"prompt": f.prompt, "ftype": f.ftype.code, "options": list(f.options),
                               "opt_values": list(f.opt_values)}
                              for f in form_read.extract_fields(page)]
                    status = FormSweepStatus.OK if fields else FormSweepStatus.EMPTY
                except Exception as e:
                    status = FormSweepStatus.ERROR
                    log.warning("[{}] свип-ошибка: {}", vid, repr(e)[:120])
            store.cache_form(vid, rec.get("name", ""), url, fields, status.code)
            swept += 1
            log.info("[{}/{}] {}: {} полей ({})", i, len(todo), vid, len(fields), status.code)
    log.info("Свип завершён: снято {} форм в кеш (forms_cache.json)", swept)
    return {"queue": len(queue), "swept": swept, "cached": len(cached)}


def clean_queue() -> dict[str, Any]:
    """Вычистить форм-очередь БЕЗ браузера: мёртвые (кеш status = empty/error — истёкшие/без формы)
    и уже откликнутые (в applied_log/marks) — чтобы не гонять зря и НЕ слать повторный отклик."""
    q = store.forms()
    cache = store.form_cache()
    applied = store.applied_ids()
    marks = store.marks()
    dead = [v for v in q
            if (s := FormSweepStatus.from_code(cache.get(v, {}).get("status"))) and s.is_dead]
    done = [v for v in q if v in applied or marks.get(v) in SETTLED_MARKS]
    # Вне целевой специализации — ТЕМИ ЖЕ правилами, что и отбор кандидатов
    # (`candidates.out_of_scope`), а не своим набором: очередь копила то, на что отклик всё
    # равно не пошёл бы. Замер 07.08.2026: из 78 накопленных 32 были QA, аналитиками
    # и руководителями — они лежали здесь неделями и попадали в каждый прогон.
    scope: dict[str, OutOfScope] = {}
    for v, meta in q.items():
        why = out_of_scope(str((meta or {}).get("name") or ""))
        if why is not None:
            scope[v] = why
    remove = set(dead) | set(done) | set(scope)
    for v in remove:
        store.remove_form(v)
    left = len(q) - len(remove)
    log.info("Форм-очередь очищена: удалено {} (мёртвых {}, уже откликнутых {}, "
             "вне специализации {}), осталось {}",
             len(remove), len(dead), len(done), len(scope), left)
    if scope:
        log.info("  вне специализации по причинам: {}",
                 dict(Counter(r.label for r in scope.values())))
    return {"removed": len(remove), "dead": len(dead), "applied": len(done),
            "out_of_scope": len(scope), "left": left}


def run(dry: bool = False, only: str = "", headless: bool = False,
        cover_mode: str = "template", limit: int = 0) -> dict[str, Any]:
    """Обработать НАКОПЛЕННУЮ форм-очередь тем же авто-путём, что inline apply: полные анкеты ->
    заполнить + «Откликнуться»; пробелы -> лог. `--dry` — только резолвинг; `limit` — максимум
    ОТПРАВЛЕННЫХ за запуск (дренаж бэклога батчами, не одним залпом).

    Суточный потолок HH (`store.daily_cap`) уважается ТАК ЖЕ, как в `autoclick._drain_pending`:
    `limit` — это размер батча, а не защита от лимита площадки (CLI-дефолт `0` = безлимит)."""
    queue = store.forms()
    if only:
        queue = {k: v for k, v in queue.items() if k == str(only)}
    if not queue:
        log.info("Форм-очередь пуста")
        return {"forms": 0, "submitted": 0}
    if not dry and not FORMS_ENABLED:
        log.error("forms: авто-обработка требует FORMS_LLM=1 (или запусти с --dry для превью)")
        return {"forms": len(queue), "submitted": 0}
    applied, marks = store.applied_ids(), store.marks()    # защита от повторного отклика

    from playwright.sync_api import sync_playwright

    from hrwork.application.apply.autoclick import (
        _goto,
        _launch,
        _logged_in,
        _page,
        _single_instance,
        is_captcha,
    )
    submitted = 0
    with _single_instance(), sync_playwright() as p:
        ctx = _launch(p, headless)
        page = _page(ctx)
        with contextlib.suppress(Exception):           # кап навигации: медленная форма не стопорит
            page.set_default_navigation_timeout(25_000)
            page.set_default_timeout(20_000)
        if not _logged_in(page):
            log.error("Нет сессии HH — сначала: hh.py autoclick --login")
            return {"forms": len(queue), "submitted": 0}
        cap = store.daily_cap()
        for vid, rec in queue.items():
            # Суточная квота ПРОВЕРЯЕТСЯ, а не только инкрементится. Дренаж — такой же
            # реальный отклик, как крон-путь, и до 08.08.2026 единственным ограничителем был
            # `--limit` с CLI-дефолтом 0 (безлимит): вечерний `hh.py forms` при выбранных
            # 200/200 добавлял сверху всю очередь. Ровно так 28.07 пробили лимит HH и словили
            # капчу — тогда чинили УЧЁТ (bump_quota), а гейт поставить забыли.
            if not dry and (used := store.applied_today()) >= cap:
                log.warning("Дневной лимит откликов исчерпан: {}/{} — дренаж форм ОСТАНОВЛЕН, "
                            "очередь остаётся до завтра", used, cap)
                break
            if not dry and limit and submitted >= limit:
                log.info("Лимит отправки достигнут: {} за запуск", limit)
                break
            if not dry and (vid in applied or marks.get(vid) in SETTLED_MARKS):
                log.info("Пропуск {}: уже откликались/отказ — не шлём", vid)
                continue
            if not _is_hh(rec.get("url") or ""):
                log.warning("Пропуск {}: URL не hh.ru ({})", vid, rec.get("url"))
                continue
            if not _goto(page, rec["url"]):
                log.warning("Пропуск {}: страница не открылась", vid)
                continue
            if is_captcha(page):
                # без этого прогон принимал страницу капчи за анкету без полей и молотил
                # очередь до конца, укрепляя бот-флаг (28.07: 21 отклик -> стена -> 50 пустых)
                log.error("HH показал капчу (/account/captcha) — прогон ОСТАНОВЛЕН на {}. "
                          "Пройди проверку вручную: hh.py forms --headed --dry --only {}", vid, vid)
                break
            _open_form(page)                               # карточка -> форма с вопросами (не submit)
            if dry:
                _dry_preview(page, vid, rec)
                continue
            cand = SimpleNamespace(id=vid, name=rec.get("name", ""))
            if try_autofill(page, cand, cover_mode):
                page.wait_for_timeout(2_500)
                store.remove_form(vid)
                store.mark_applied(vid)
                # Квота и журнал — как в apply-пути. Без этого дренаж был НЕВИДИМ для суточного
                # потолка: 28.07 счётчик показывал 35 при реально отправленных ~103, поэтому
                # HH_DAILY_APPLY_CAP не защитил и мы упёрлись в лимит HH. В журнал такие отклики
                # попадали лишь позже — синком из чатов и с чужим каналом `hh`.
                store.bump_quota(1)
                # employer — из кеша вакансий, а не из rec: в форм-очереди лежат только
                # {name, url, ts}. Без него запись журнала уходит с пустым работодателем, и
                # когда вакансия выпадет из выдачи, её карточка-«призрак» в ленте не найдётся
                # поиском по компании (01.08.2026, docs/errors.md).
                store.log_applied(vid, rec.get("name", ""), rec.get("url", ""),
                                  via=ApplyChannel.CRON, employer=_vacancy_ctx(vid)[0])
                submitted += 1
                time.sleep(random.uniform(*FORM_PAUSE))    # см. FORM_PAUSE: темп важнее скорости
    log.info("Форм-очередь: {} обработано, {} откликов отправлено", len(queue), submitted)
    return {"forms": len(queue), "submitted": submitted}
