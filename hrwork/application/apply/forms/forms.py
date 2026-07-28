"""Авто-заполнение форм-опросников HH (часть отклика, RFC-003 + авто-submit).

Форма — это ЧАСТЬ отклика: заполнение живёт inline в `autoclick.apply_one` (когда отклик
упирается в анкету и включён FORMS_LLM). Здесь — переиспользуемое ядро `try_autofill(page, cand)`:
резолвит ВСЕ поля (словарь/факты/LLM); при полноте заполняет + пишет письмо + ЖМЁТ «Откликнуться»;
хоть один пробел -> НЕ шлёт, лог пробелов -> в форм-очередь (человек пополняет form_answers).

`run()` — тем же путём обрабатывает НАКОПЛЕННУЮ форм-очередь (`store.forms()`): бэклог, что
осел до inline или где пробелы уже закрыты словарём. `--dry` — только показать резолвинг.

БЕЗОПАСНОСТЬ (docs/security.md): у LLM нет execution-канала (только строка -> .fill/.check/
.select_option/print) — машина не тронута ни при какой инъекции. Ответы только из фактов/словаря;
radio/checkbox строго из опций (membership). origin только hh.ru.
"""
from __future__ import annotations

import contextlib
import random
import time
from types import SimpleNamespace
from urllib.parse import urlparse

from hrwork.application.apply import cover
from hrwork.application.apply.chat import chat_answer
from hrwork.application.apply.forms import form_fill, form_read
from hrwork.application.apply.forms.form_status import FormSweepStatus
from hrwork.application.apply.outcome import ApplyChannel
from hrwork.application.apply.runtime.store import store
from hrwork.config import FORMS_ENABLED, log

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


_VAC_CTX: dict | None = None       # ленивый кеш {id: (employer, desc, name, salary_floor)}


def _load_vac_ctx() -> dict:
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
                _VAC_CTX[r.vacancy.id] = (r.vacancy.employer or "",
                                          getattr(r, "requirement", "") or "", r.vacancy.name or "", floor)
    return _VAC_CTX


def _vacancy_ctx(vid: str) -> tuple[str, str, str]:
    v = _load_vac_ctx().get(str(vid))
    return v[:3] if v else ("", "", "")


def _vacancy_floor(vid: str) -> int | None:
    v = _load_vac_ctx().get(str(vid))
    return v[3] if v else None


def _resolve(field, resume_ctx: str, sal_target: int | None = None,
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


def _fill_field(page, field, value: str, own: str | None = None) -> bool:
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


def _fill_cover(page, vid: str, rec: dict, cover_mode: str) -> None:
    """Сопроводительное — в поле письма на самой форме (не через chatik). Textarea спрятан за
    тоглом «Добавить» (`_LETTER_TOGGLE`, выверено живым прогоном 134804227) — сперва раскрыть."""
    emp, desc, nm = _vacancy_ctx(vid)
    cand = SimpleNamespace(id=str(vid), name=nm or rec.get("name", ""), employer=emp, desc=desc)
    with contextlib.suppress(Exception):
        if not page.locator(_LETTER).count():
            page.locator(_LETTER_TOGGLE).first.click(timeout=3_000)
            page.wait_for_timeout(500)
        page.locator(_LETTER).first.fill(cover.build_cover(cand, cover_mode))
        log.info("[{}] сопроводительное вписано в поле формы", vid)
        return
    log.warning("[{}] письмо НЕ вписано (поле не найдено/fill упал) — отклик уйдёт без него", vid)


def _submitted(page) -> bool:
    """Отклик реально ушёл: появился чат-топик или «Вы откликнулись» (иначе не метим applied)."""
    with contextlib.suppress(Exception):
        return bool(page.locator('[data-qa="vacancy-response-link-view-topic"]').count()
                    or page.get_by_text("Вы откликнулись").count())
    return False


def _wait_submitted(page, tries: int = 6) -> bool:
    """Поллинг подтверждения до ~12с: HH перерисовывает карточку с лагом (инцидент 134804227,
    2026-07-22 — отклик УШЁЛ, но за 2с подтверждение не успело, прогон счёл его неотправленным)."""
    for _ in range(tries):
        page.wait_for_timeout(2_000)
        if _submitted(page):
            return True
    return False


def try_autofill(page, cand, cover_mode: str = "template") -> bool:
    """Inline авто-отклик на анкету (вызывается из apply_one под гейтом FORMS_ENABLED, страница
    уже на форме). Резолвит ВСЕ поля; при полноте — заполняет, пишет письмо, ЖМЁТ «Откликнуться»
    -> True (верификацию делает apply_one). Пустое извлечение или ХОТЬ ОДИН пробел -> НЕ шлёт,
    лог пробелов -> False (вакансия уходит в форм-очередь, человек пополняет form_answers)."""
    resume_ctx = form_fill.build_resume_ctx()
    fields = form_read.extract_fields(page)
    if not fields:
        log.info("[{}] анкета без извлечённых полей — в очередь (руками)", cand.id)
        return False
    _, desc, nm = _vacancy_ctx(str(cand.id))      # описание — контекст для «чем интересна вакансия»
    sal_target = form_fill.salary_target(nm or getattr(cand, "name", ""), _vacancy_floor(cand.id))
    resolved = [(f, *_resolve(f, resume_ctx, sal_target, desc)) for f in fields]
    gaps = [f for f, val, _ in resolved if not val]
    if gaps:
        for f in gaps:
            log.warning("[{}] ПРОБЕЛ анкеты (нет ответа): {}", cand.id, f.prompt[:90])
        log.warning("[{}] АВТО-ОТКЛИК ПРОПУЩЕН: {}/{} полей без ответа — пополни form_answers "
                    "в resume_profile.json", cand.id, len(gaps), len(fields))
        return False
    for f, val, own in resolved:
        _fill_field(page, f, val, own)
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


def _open_form(page) -> None:
    """С карточки вакансии перейти на форму отклика: клик «Откликнуться/пройти тест» + модалка
    релокации. Клик ОТКРЫВАЕТ форму с вопросами — это НЕ отправка (submit — отдельная _SUBMIT).
    Нужен только `run()` (бэклог хранит URL карточки); в inline-пути apply_one клик уже сделан."""
    with contextlib.suppress(Exception):
        page.locator('[data-qa="vacancy-response-link-top"]').first.click(timeout=8_000)
    with contextlib.suppress(Exception):
        page.locator('[data-qa="relocation-warning-confirm"]').first.click(timeout=3_000)
    page.wait_for_timeout(1_500)


def _dry_preview(page, vid: str, rec: dict) -> None:
    """Показать поля + резолвинг для одной вакансии, ничего не трогая."""
    resume_ctx = form_fill.build_resume_ctx() if FORMS_ENABLED else ""
    fields = form_read.extract_fields(page)
    _, desc, _ = _vacancy_ctx(vid)
    sal_target = form_fill.salary_target(rec.get("name", ""), _vacancy_floor(vid))
    gaps = 0
    for f in fields:
        val, own = _resolve(f, resume_ctx, sal_target, desc)
        gaps += not val
        tail = f" (+свой: {own})" if own else ""
        log.info("  [{}] {} -> {}", f.ftype.code, f.prompt[:55],
                 (f"{val}{tail}" if val else "DECLINE (пробел)"))
    log.info("[{}] {} — полей {}, пробелов {}", vid, rec.get("name"), len(fields), gaps)


def sweep(only: str = "", headless: bool = True, refresh: bool = False) -> dict:
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
                    fields = [{"prompt": f.prompt, "ftype": f.ftype.code, "options": list(f.options)}
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


def clean_queue() -> dict:
    """Вычистить форм-очередь БЕЗ браузера: мёртвые (кеш status = empty/error — истёкшие/без формы)
    и уже откликнутые (в applied_log/marks) — чтобы не гонять зря и НЕ слать повторный отклик."""
    q = store.forms()
    cache = store.form_cache()
    applied = store.applied_ids()
    marks = store.marks()
    dead = [v for v in q
            if (s := FormSweepStatus.from_code(cache.get(v, {}).get("status"))) and s.is_dead]
    done = [v for v in q if v in applied or marks.get(v) in ("applied", "rejected")]
    remove = set(dead) | set(done)
    for v in remove:
        store.remove_form(v)
    left = len(q) - len(remove)
    log.info("Форм-очередь очищена: удалено {} (мёртвых {}, уже откликнутых {}), осталось {}",
             len(remove), len(dead), len(done), left)
    return {"removed": len(remove), "dead": len(dead), "applied": len(done), "left": left}


def run(dry: bool = False, only: str = "", headless: bool = False,
        cover_mode: str = "template", limit: int = 0) -> dict:
    """Обработать НАКОПЛЕННУЮ форм-очередь тем же авто-путём, что inline apply: полные анкеты ->
    заполнить + «Откликнуться»; пробелы -> лог. `--dry` — только резолвинг; `limit` — максимум
    ОТПРАВЛЕННЫХ за запуск (дренаж бэклога батчами, не одним залпом)."""
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
        for vid, rec in queue.items():
            if not dry and limit and submitted >= limit:
                log.info("Лимит отправки достигнут: {} за запуск", limit)
                break
            if not dry and (vid in applied or marks.get(vid) in ("applied", "rejected")):
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
                store.log_applied(vid, rec.get("name", ""), rec.get("url", ""),
                                  via=ApplyChannel.CRON)
                submitted += 1
                time.sleep(random.uniform(*FORM_PAUSE))    # см. FORM_PAUSE: темп важнее скорости
    log.info("Форм-очередь: {} обработано, {} откликов отправлено", len(queue), submitted)
    return {"forms": len(queue), "submitted": submitted}
