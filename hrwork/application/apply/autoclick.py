"""Автокликер hh.ru (Playwright): поднятие резюме + автоотклики под своим аккаунтом.

Сессия — persistent-профиль Chromium (`browser.PROFILE_DIR`, гитигнорен вместе с data/):
  1) python hh.py autoclick --login   — ОДИН раз, с окном: вход по HH_EMAIL+HH_PASSWORD
     (.env) автоматом; капчу/код (если HH попросит) вводишь руками.
  2) python hh.py autoclick           — headless: поднять резюме + отклики (по крону);
     протухшая сессия самовосстанавливается автовходом по паролю.

Когти разнесены по модулям (аудит 22.09.2026, §5 — был god-модуль на 1486 строк):
  * `browser.py`   — браузерные примитивы, вход и поднятие резюме;
  * `selectors.py` — селекторы отклика и бюджет подтверждения (один источник на оба пути);
  * `hh_sync.py`   — синк статусов из чатов (без браузера);
  * `worker.py`    — тёплый воркер для сервера ленты;
  * `runtime/watchdog.py` — дедлайн зависшего прогона.
Здесь остаётся САМ ОТКЛИК: `apply_one` и его селекторы/тайминги (прямой запрет CLAUDE.md —
«выстраданы инцидентами»), батч, дренаж очереди и вход `run`.

Playwright — опциональная зависимость (паттерн psycopg2 у поиска): импорт ленивый,
без него остальные режимы hh.py работают, а отбор (`candidates.pick_candidates`) тестируется
без браузера.
"""
import contextlib
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from hrwork.application.apply import (
    ab_split,
    account_session,
    browser,
    cover,
    hh_sync,
    selectors,
    taken,
)
from hrwork.application.apply.candidates import (
    REASON_DEVELOPER_TITLE,
    REASON_EMPLOYER_BLOCKED,
    REASON_ROLE_NOT_ALLOWED,
    REASON_TAKEN_BY_OTHER,
    Candidate,
    employer_blocked,
    pick_candidates,
)
from hrwork.application.apply.chat import chat
from hrwork.application.apply.forms.form_status import skippable_form_ids
from hrwork.application.apply.outcome import ApplyChannel, ApplyOutcome, VacancyMark
from hrwork.application.apply.runtime import bump_state, lock, quota, watchdog
from hrwork.application.apply.runtime.quota import DAILY_CAP_DEFAULT
from hrwork.application.apply.runtime.store import store
from hrwork.config import (
    ACCOUNT,
    APPLY_SKIP_STREAK_MAX,
    APPLY_UNCONFIRMED_STREAK_MAX,
    FORMS_ENABLED,
    HH_APPLY_ROLLING_CAP,
    log,
)
from hrwork.infrastructure.storage import vacancy_repository

# ── Отклик: параметры батча (отбор кандидатов -> candidates.pick_candidates) ──
APPLY_LIMIT_DEFAULT = 10                                 # откликов за один запуск
APPLY_PAUSE = (4.0, 9.0)   # пауза между откликами, сек — не долбим HH очередями
POOL_MULT = 5              # пул кандидатов = eff × POOL_MULT (запас на пропуски: опросник/архив/уже)


# Маркеры АРХИВНОЙ вакансии (одна CSS-строка с запятой, не кортеж — см. selectors.py).
# Сверено 04.08.2026 на живых страницах: оба есть у архивных и отсутствуют у активных.
# Отличать архив от «кнопки нет по другой причине» обязательно: под общим ярлыком
# «внешний/архив» три недели пряталось обязательное сопроводительное письмо.
_ARCHIVED = ('[data-qa="vacancy-archive-description"], '
             '[data-qa="vacancy-title-archived-text"]')


def _submit_diag(page: Any) -> str:
    """Почему подтверждение отклика не пришло — одной строкой в лог.

    До 03.08.2026 эта ветка возвращала SKIP МОЛЧА, и отказ был неотличим от архива: на 50
    пропусков в логе приходилось 3 строки «кнопки отклика нет», остальные 47 не оставляли
    ничего. Так деградация с 2.8 до 62 пропусков на отклик шла три недели незамеченной.
    Только чтение DOM и всё под suppress: после клика страница может быть в любом состоянии,
    и диагностика не имеет права уронить прогон."""
    bits: list[str] = []
    with contextlib.suppress(Exception):
        bits.append(f"url={page.url}")
    with contextlib.suppress(Exception):
        btn = page.locator(selectors.RESPONSE_SUBMIT).first
        if not btn.count():
            bits.append("сабмит=нет")
        else:
            bits.append("сабмит=disabled" if btn.is_disabled(timeout=1_000) else "сабмит=активен")
    with contextlib.suppress(Exception):
        letter = page.locator(selectors.RESPONSE_LETTER).first
        if letter.count():
            bits.append("письмо=" + ("ПУСТО" if not (letter.input_value(timeout=1_000) or "").strip()
                                     else "заполнено"))
    return ", ".join(bits) or "состояние страницы недоступно"


def _fill_letter_if_required(page: Any, cand: Candidate,
                             cover_text: str = "", cover_mode: str = "template") -> bool:
    """Вписать сопроводительное, если без него HH не даёт откликнуться.

    Часть работодателей помечает письмо обязательным: поле пустое -> кнопка «Откликнуться»
    приходит с `disabled`, клик по ней падает по таймауту, и отклик молча не уходит. Замер
    03.08.2026: 11 из 11 «архивных» вакансий были живые (`active=true`) именно с этим.

    Заполняем ТОЛЬКО когда кнопка disabled — там, где HH пускает и так, поведение прежнее
    (письмо по-прежнему уходит в СЛОТ сопроводительного через chatik, см. _send_cover_via_chat).
    Текст строится ЛЕНИВО: сюда доходят только живые вакансии, где отклик реально идёт,
    поэтому режим 'llm' не тратит запрос на архив и пропуски.

    ГРАНИЦА SUPPRESS. Подавляются ТОЛЬКО обращения к DOM (чужое: страница живёт своей жизнью,
    контекст рушится). Построение письма вынесено наружу СОЗНАТЕЛЬНО (аудит 08.08.2026): пока
    `cover.build_cover` лежал под тем же suppress, НАШ AttributeError/TypeError давал молчаливый
    False -> обязательное письмо не вписано -> кнопка осталась disabled -> «отклик НЕ подтверждён
    за 12с», и выглядело это как проблема HH. Тот же класс уже стоил трёх недель (docs/errors.md,
    03–04.08.2026). Теперь наша ошибка летит наверх: её видно строкой «<название> (<id>): <ошибка>»
    из `_apply_batch` и возвратом записи в очередь из `_drain_pending`.

    Восстановление: наружу ничего не коммитится — поле либо заполнено, либо нет; повтор
    безопасен (второй fill пишет то же значение). Наблюдаемость: строка «письмо обязательно —
    вписал…» на успехе и WARNING «поле не заполнилось» на отказе DOM."""
    need = False
    with contextlib.suppress(Exception):        # ЧУЖОЕ: чтение DOM
        btn = page.locator(selectors.RESPONSE_SUBMIT).first
        letter = page.locator(selectors.RESPONSE_LETTER).first
        # письмо требуется, только если кнопка disabled И поле письма на странице есть:
        # активная кнопка — не наш случай, disabled без поля — тоже
        need = bool(btn.count() and btn.is_disabled(timeout=1_000) and letter.count())
    if not need:
        return False
    # НАШЕ: текст письма. Пробельный текст письмом не считаем — disabled он не снимет, а поле затрёт.
    text = (cover_text or "").strip() or cover.build_cover(cand, cover_mode).strip()
    if not text:
        return False
    filled = False
    with contextlib.suppress(Exception):        # ЧУЖОЕ: запись в DOM
        page.locator(selectors.RESPONSE_LETTER).first.fill(text, timeout=3_000)
        filled = True
    if filled:
        log.info("{}: письмо обязательно — вписал в форму отклика ({} симв.)", cand.id, len(text))
    else:
        log.warning("{}: письмо обязательно, но поле не заполнилось — отклик, вероятно, не уйдёт",
                    cand.id)
    return filled


def _page_unreadable(page: Any) -> bool:
    """URL страницы не читается (контекст рухнул / идёт навигация) — состояние НЕИЗВЕСТНО.

    Отдельный сигнал, а НЕ изменение `is_captcha`: у той по решению владельца нечитаемая
    страница считается «не капчей» (`test_captcha_guard.py::test_unreadable_page_is_not_captcha`
    — во время навигации чтение url бросает, и это не повод объявлять капчу). Но это и не повод
    искать кнопку отклика ВСЛЕПУЮ: асимметрия цены (аудит 22.09.2026, §1) — ложное «капча»
    стоит одного пропущенного слота, ложное «не капча» это инцидент 28.07 (35 карточек и
    полсотни анкет, «перемолотых» под капчей, каждый клик — бот-сигнал HH)."""
    try:
        _ = page.url
    except Exception:
        return True
    return False


def apply_one(page: Any, cand: Candidate, cover_text: str = "",
              cover_mode: str = "template") -> ApplyOutcome:
    """Отклик на одну вакансию (письмо — только если HH требует его для отправки). Исход:
      APPLIED — отклик отправлен; ALREADY — уже откликались (кнопка заменена на «Чат»);
      FORM — вакансия с вопросами работодателя (в форм-очередь, руками);
      CAPTCHA — HH увёл на проверку, дальше идти бессмысленно;
      SKIP — откликнуться НЕЛЬЗЯ: архив, внешний сайт, опросник;
      UNCONFIRMED — клик был, подтверждения нет (потолок 24ч HH либо `disabled` сабмит)."""
    browser._goto(page, cand.url)
    if _page_unreadable(page):
        # ни клика, ни поиска кнопки: состояние страницы неизвестно (см. `_page_unreadable`)
        log.warning("{}: состояние страницы неизвестно (url не читается) — пропускаю, "
                    "вслепую не кликаю", cand.id)
        return ApplyOutcome.SKIP
    if browser.is_captcha(page):
        return ApplyOutcome.CAPTCHA
    btn = page.locator('[data-qa="vacancy-response-link-top"]').first
    try:
        btn.wait_for(timeout=8_000)
    except Exception:
        # кнопки «Откликнуться» нет — уже откликались, архив или что-то ещё
        if page.locator('[data-qa="vacancy-response-link-view-topic"]').count():
            return ApplyOutcome.ALREADY
        archived = False
        with contextlib.suppress(Exception):
            archived = bool(page.locator(_ARCHIVED).count())
        log.info("{}: {}", cand.id, "вакансия в архиве" if archived else
                 "кнопки отклика нет, и это НЕ архив — внешний сайт либо смена вёрстки HH")
        return ApplyOutcome.SKIP
    is_survey = bool(re.search(r"тест|опрос", btn.inner_text().lower()))
    if is_survey and not FORMS_ENABLED:                 # OFF (крон-дефолт) — как раньше, в очередь
        # INFO, а не DEBUG: ветка срабатывает именно на кроне (FORMS_ENABLED там может быть
        # выключен), и на DEBUG исход FORM не попадал в крон-лог вовсе — массовое включение
        # опросников на HH было неотличимо от «просто мало откликов» (аудит 08.08.2026).
        log.info("{}: отклик с опросником — в форм-очередь", cand.id)
        return ApplyOutcome.FORM
    btn.click()
    # модалка «вакансия в другом городе/стране»
    with contextlib.suppress(Exception):
        page.locator('[data-qa="relocation-warning-confirm"]').first.click(timeout=3_000)
    # клик мог увести на форму отклика с ВОПРОСАМИ работодателя (/applicant/vacancy_response,
    # data-qa=task-question). При FORMS_LLM=1 — заполняем анкету inline (часть отклика);
    # иначе / при пробеле извлечения — в форм-очередь (человек), не ждём бюджет подтверждения.
    page.wait_for_timeout(1_500)
    has_form = False
    with contextlib.suppress(Exception):
        has_form = bool("/applicant/vacancy_response" in page.url
                        and page.locator('[data-qa="task-question"], textarea[name^="task_"]').count())
    if has_form or is_survey:
        if not FORMS_ENABLED:
            log.info("{}: отклик с вопросами работодателя — в форм-очередь", cand.id)
            return ApplyOutcome.FORM
        from hrwork.application.apply.forms import forms
        if not forms.try_autofill(page, cand):                 # пробел/пусто -> в очередь (человек)
            return ApplyOutcome.FORM
        # try_autofill заполнил и нажал «Откликнуться» -> верифицируем общим блоком ниже
    else:
        _fill_letter_if_required(page, cand, cover_text, cover_mode)
        with contextlib.suppress(Exception):        # обычный отклик — ОДИН локатор, не перебор!
            page.locator(selectors.RESPONSE_SUBMIT).first.click(timeout=3_000)
    if selectors.wait_response_confirmed(page):
        return ApplyOutcome.APPLIED
    diag = _submit_diag(page)           # ДО перезагрузки: после неё страница уже другая
    if _confirmed_after_reload(page, cand.url):
        log.info("{}: подтверждение не пришло за {}с ({}), но после перезагрузки вакансия "
                 "показывает отклик — засчитан", cand.id, selectors.CONFIRM_BUDGET_S, diag)
        return ApplyOutcome.APPLIED
    log.warning("{}: отклик НЕ подтверждён за {}с — {}", cand.id, selectors.CONFIRM_BUDGET_S, diag)
    return ApplyOutcome.UNCONFIRMED


def _confirmed_after_reload(page: Any, url: str) -> bool:
    """Перепроверка неподтверждённого клика: открыть вакансию заново и поискать маркер
    «Вы откликнулись» — тот же, по которому прогон узнаёт «уже откликались».

    Зачем (сверка логов с журналом 24.09.2026): из «НЕ подтверждён» у acc2 5 из 131, у основного
    49 из 400 на самом деле УШЛИ — синк дожурналил их из чатов с временем HH, совпадающим с
    кликом, у всех диагностика «сабмит=нет» (поп-ап не появился, HH принял отклик сразу, но
    карточку за бюджет не перерисовал). Прогон не засчитывал их в цель и слал ещё один отклик
    сверх лимита, а квота догоняла факт только на синке.

    Только GET той же страницы, повторного клика нет. Упор в потолок 24ч маркера не даёт, и
    исход остаётся UNCONFIRMED: серию по-прежнему рвёт APPLY_UNCONFIRMED_STREAK_MAX, то есть
    лишних перезагрузок не больше пяти подряд. Любой сбой навигации — False (как было).
    После `_goto` гарантирован только корень приложения, а блок отклика HH дорисовывает позже:
    ждём кнопку ИЛИ маркер до 8 с — то же ожидание, что у распознавания «уже откликались» на
    входе в `apply_one`."""
    try:
        browser._goto(page, url)
    except Exception:
        return False
    with contextlib.suppress(Exception):
        page.locator(f'{selectors.RESPONSE_DONE_MARKER}, '
                     '[data-qa="vacancy-response-link-top"]').first.wait_for(timeout=8_000)
    return selectors.response_confirmed(page)


def _sync_applied_from_chats(ctx: Any, page: Any) -> int:
    """Вакансии с чатом на HH = уже откликнутые -> отметить в marks.json. Cookie-only
    (chatik.hh.ru без fingerprint), чинит дрейф marks<->HH навсегда. Возвращает число
    новых отметок."""
    xsrf = ""
    with contextlib.suppress(Exception):
        xsrf = {c["name"]: c["value"] for c in ctx.cookies()}.get("_xsrf", "")
    ids = chat.applied_vacancy_ids(page.context.request, xsrf)
    if not ids:
        return 0
    marks = store.marks()
    fresh = {vid: VacancyMark.APPLIED.code for vid in ids if vid not in marks}
    if fresh:
        store.merge_marks(fresh)
        log.info("Синхронизировано откликов из чатов: +{} (в marks стало {})",
                 len(fresh), len(marks) + len(fresh))
    return len(fresh)


def _send_cover_via_chat(page: Any, cand: Candidate, text: str) -> bool:
    """Записать письмо в СЛОТ СОПРОВОДИТЕЛЬНОГО (chatik /save правит сообщение-отклик, а не
    шлёт новое сообщение — иначе у работодателя это не сопроводительное, а реплика в чат).
    Cookie-only, без fingerprint. Чат/сообщение-отклик создаются не мгновенно — пара попыток."""
    if not text:
        return False
    xsrf = ""
    with contextlib.suppress(Exception):
        xsrf = {c["name"]: c["value"] for c in page.context.cookies()}.get("_xsrf", "")
    req = page.context.request
    for _ in range(4):
        cid, aid = chat.find_chat(req, xsrf, cand.id)
        if cid and aid:
            mid = chat.cover_message_id(chat.chat_data(req, xsrf, cid, aid))
            if mid:
                ok = chat.save_cover(req, xsrf, cid, mid, text)
                log.info("{}: сопроводительное в чат {} (msg {}) — {}",
                         cand.id, cid, mid, "сохранено" if ok else "ошибка")
                return ok
        page.wait_for_timeout(2_000)               # чат/слот ещё не готовы — подождём
    log.info("{}: слот сопроводительного не найден — письмо не записано", cand.id)
    return False


# Анкета — не отклик и не пропуск, поэтому в отношении «скип/отклик» её не видно вовсе.
# Сценарий из аудита: HH массово включает опросники, прогон даёт 3 отклика и 20 анкет,
# «пропусков 0», отношение в норме — и падение темпа не видно неделями. Порог по смыслу
# тот же, что у пропусков: анкет вдвое больше откликов -> сводка уходит в WARNING.
SKIP_RATIO_WARN = 5.0
FORM_RATIO_WARN = 2.0


def _run_summary(applied: int, skipped: int, forms: int, reconciled: int,
                 letters_failed: int = 0, unconfirmed: int = 0) -> tuple[str, str]:
    """Уровень и текст итоговой строки прогона: ('warning'|'info', строка).
    Вынесено из `_apply_batch` чистой функцией — это ЕДИНСТВЕННЫЙ сигнал деградации,
    и он обязан проверяться тестом без браузера.

    `letters_failed` — сколько откликов ушло БЕЗ сопроводительного (слот чата не найден/ошибка
    записи). До 22.09.2026 батч отбрасывал возврат `_send_cover_via_chat`: работодатель получал
    отклик без письма, а в сводке этого не было вовсе (аудит §1).

    `unconfirmed` — сколько попыток осталось БЕЗ подтверждения от HH (клик был). В сводке он
    печатается, но сам по себе строку в warning НЕ переводит: единичный случай — норма, а
    серия рвёт прогон предохранителем APPLY_UNCONFIRMED_STREAK_MAX. Число в сводке нужно,
    чтобы упор в потолок HH был виден в эксплуатации, а не только по серии."""
    ratio = f"{skipped / applied:.1f}" if applied else "все"
    line = (f"Итог прогона: откликов {applied}, анкет {forms}, пропусков {skipped} "
            f"(скип/отклик {ratio}), уже откликались {reconciled}")
    if unconfirmed:
        line += f", не подтвердилось {unconfirmed}"
    if letters_failed:
        line += f", писем не доставлено {letters_failed}"
    hot = (not applied
           or skipped / max(applied, 1) >= SKIP_RATIO_WARN
           or forms / max(applied, 1) >= FORM_RATIO_WARN
           or letters_failed > 0)
    return ("warning" if hot else "info"), line


# Имя и работодатель вакансии по id — СНИМОК репозитория, а не растущий кеш процесса: наполняет
# `_remember_vacancy_meta` (крон-батч УЖЕ загрузил реестр), читают `_apply_one_vacancy` и дренаж.
# Ради одной строки журнала 480-МБ кеш вакансий заново НЕ читаем: та же причина, по которой
# `forms.clean_queue` судит только по тайтлу (docs/apply.md).
#
# ПОЧЕМУ СНИМОК (аудит 22.09.2026, §5): раньше это был module-level `update`, то есть множество,
# растущее от прогона к прогону в долгоживущем процессе, без инвалидации. Теперь набор
# ЗАМЕНЯЕТСЯ на каждом батче (предел = размер реестра, ~129k записей) и сбрасывается по
# завершении `run()`. Вытеснять по размеру НЕЛЬЗЯ: пропуск записи дал бы пустого работодателя
# в журнале — регресс 01.08.2026 («карточка-призрак» не находится по компании).
_VACANCY_META: dict[str, tuple[str, str]] = {}


def _remember_vacancy_meta(records: list[Any]) -> None:
    """Заменить снимок (имя, работодатель) по id из уже загруженных записей репозитория."""
    _VACANCY_META.clear()
    _VACANCY_META.update({r.id: (r.vacancy.name, r.vacancy.employer or "") for r in records})


# Строки отсева, ради которых заведена эксплуатационная наблюдаемость RFC-004 («отсев
# блок-листа/ролей/аккаунтов»). Собираются из КОНСТАНТ `candidates.py`, а не копируются:
# переформулировка причины в отборе молча гасила эту строку (аудит 22.09.2026, §3.2).
_POOL_WATCH_REASONS = frozenset({
    REASON_EMPLOYER_BLOCKED, REASON_ROLE_NOT_ALLOWED, REASON_DEVELOPER_TITLE,
    REASON_TAKEN_BY_OTHER,
    ab_split.REASON_FOREIGN,
})


def _apply_batch(page: Any, apply_limit: int | None, daily_cap: int,
                 cover_mode: str = "template") -> int:
    """Разослать отклики с сопроводительным письмом, с учётом ДВУХ потолков HH: дневного
    (~200/сутки) и скользящих 24ч (~45, config.HH_APPLY_ROLLING_CAP — эмпирика, HH её не
    документирует). Эффективный лимит запуска = min(apply_limit, дневной_остаток, остаток
    окна) — так N мелких запусков за день суммарно не превышают cap (идемпотентно: счётчик в
    apply_quota.json, дубли режет marks.json), а прогон не кликает в стену, где HH уже не
    подтверждает отклики. Письмо: шаблон или LLM (cover_mode)."""
    used = hh_sync._reconciled_today()
    remaining = max(0, daily_cap - used)
    eff = min(apply_limit or APPLY_LIMIT_DEFAULT, remaining)
    if eff <= 0:
        log.info("Дневной лимит откликов исчерпан: {}/{} — пропускаю отклики", used, daily_cap)
        return 0

    # Второй потолок HH — скользящие сутки (config.HH_APPLY_ROLLING_CAP). Дневная квота выше
    # его НЕ видит: 23.09.2026 счётчик показывал 189 свободных из 200, а HH уже не подтверждал
    # ни один отклик — прогон прошёл 33 вакансии впустую. Считаем по журналу (он и про вчерашний
    # вечер знает, в отличие от apply_quota.json) и урезаем ЦЕЛЬ прогона остатком окна: кликать
    # в стену дороже, чем не добрать откликов, — каждый клик это бот-сигнал.
    rolling = quota.applied_in_window(store.applied_log())
    room = max(0, HH_APPLY_ROLLING_CAP - rolling)
    if room <= 0:
        log.error("За скользящие {}ч откликов {} (потолок HH ~{}) — выше него HH не подтверждает "
                  "отклики. Прогон НЕ начинаю: пустая серия кликов = бот-сигналы. Окно "
                  "(журнал applied_log.jsonl) просядет само; пул подхватит следующий слот.",
                  quota.ROLLING_WINDOW_H, rolling, HH_APPLY_ROLLING_CAP)
        return 0
    if room < eff:
        log.warning("Окно {}ч: откликов {} из {} — цель прогона урезана до {}",
                    quota.ROLLING_WINDOW_H, rolling, HH_APPLY_ROLLING_CAP, room)
        eff = room

    # eff — целевое число УСПЕШНЫХ откликов; пропуски (внешний/уже-откликнутые) его НЕ тратят.
    # Берём пул с запасом (×POOL_MULT) и идём по нему, пока не наберём eff.
    # Вакансии-опросники исключаем ПО ФОРМ-ОЧЕРЕДИ (не через marks): бот их не заполняет,
    # а без исключения очередь упиралась в них каждый прогон.
    # Пропускаем не всю форм-очередь, а только те анкеты, что ещё имеют смысл пропускать:
    # мёртвая форма означает снятую вакансию, но если её ПЕРЕОТКРЫЛИ после свипа — форма
    # могла ожить, и вакансия возвращается в оборот сама (form_status.skippable_form_ids).
    records = vacancy_repository().load()
    _remember_vacancy_meta(records)    # дренаж очереди журналирует работодателя без второй загрузки
    published = {r.id: r.vacancy.published_at for r in records}
    forms = skippable_form_ids(store.forms(), store.form_cache(), published)
    if not ACCOUNT.is_main:
        # Пул acc2 — по правилам ЭТОГО аккаунта и текущему срезу кеша, без marks и журнала:
        # основной вычитает его целиком и сверяет свежесть по штампу среза (ab_split.py).
        ab_split.write_eligible(c.id for c in pick_candidates(records, ab_split.PoolInputs(),
                                                              len(records)))
    split = ab_split.current_split()                 # None — аккаунт один или у acc2 нет eligible
    # RFC-004: единый источник входов пула для боевого прогона и превью --dry-pool. Основной —
    # общие marks + журналы других + вычитание пула acc2; приоритетный acc2 — дедуп только против
    # своей истории, никем не блокируется, берёт весь свой пул (включая ground основного).
    inputs = ab_split.pool_inputs(split)
    stats: dict[str, int] = {}
    pool = pick_candidates(records, inputs, eff * POOL_MULT, form_ids=forms, stats=stats)
    log.info("Пул кандидатов [{}]: {} (цель {} новых откликов, дневной остаток {}/{}, "
             "опросников пропущено: {}, письмо: {})",
             ACCOUNT.code, len(pool), eff, remaining, daily_cap, len(forms), cover_mode)
    # Отсев RFC-004 — отдельной строкой и только если был: по ней видно в эксплуатации, что
    # сплит, блок-лист и дедуп аккаунтов работают, а не молча съели пул.
    rfc004 = {k: n for k, n in stats.items() if k in _POOL_WATCH_REASONS}
    if rfc004:
        log.info("  отсев блок-листа/ролей/аккаунтов: {}", rfc004)

    applied: dict[str, str] = {}       # НОВЫЕ отклики (идут в квоту)
    reconciled: dict[str, str] = {}    # уже откликались ранее (только синхронизация marks)
    skipped = 0                        # всего пропусков за прогон (для сводки в конце)
    forms_n = 0                        # анкет отложено за прогон (в сводку: не отклик и не пропуск)
    letters_failed = 0                 # писем НЕ доставлено (в сводку: отклик ушёл без письма)
    streak = 0                         # ПОДРЯД идущих пропусков — детектор блокировки
    unconfirmed = 0                    # попыток без подтверждения HH за прогон (в сводку)
    unconfirmed_streak = 0             # ПОДРЯД идущих таких попыток — упор в потолок HH
    for cand in pool:
        if len(applied) >= eff:        # набрали нужное число НОВЫХ откликов — стоп
            break
        # RFC-004: пул собран минуты назад, а другой аккаунт или ручной клик в ленте мог занять
        # вакансию за это время. Проверка ПЕРЕД кликом, с диска; в серию пропусков не идёт —
        # это не признак холостого прогона. Только у основного: acc2 — приоритетный, никем не
        # блокируется, а его собственную историю уже вычел own_handled_ids на этапе пула.
        if ACCOUNT.is_main and (owner := taken.taken_by_others().get(cand.id)) is not None:
            log.warning("{}: уже занята аккаунтом {} — не кликаю", cand.id, owner)
            continue
        status = ApplyOutcome.SKIP
        try:
            status = apply_one(page, cand, cover_mode=cover_mode)
        except Exception as e:
            log.warning("{} ({}): {}", cand.name, cand.id, e)
        if status is ApplyOutcome.APPLIED:
            applied[cand.id] = VacancyMark.APPLIED.code
            # marks/квоту/журнал пишем СРАЗУ (как feed-путь _apply_one_vacancy), а не в конце батча:
            # зависание/kill посреди прогона раньше ТЕРЯЛ отметки всех успешных откликов
            # (18.07: 21 реальный отклик ушёл, marks/квота — нет; спасал лишь чат-синк).
            # Тройка «отметка -> квота -> журнал» — в ОДНОЙ точке `store.commit_applied`
            # (аудит 22.09.2026, §1): три копии уже разошлись по составу полей.
            total = store.commit_applied(
                cand.id, name=cand.name, url=cand.url, employer=cand.employer,
                via=ApplyChannel.CRON,
                ab=split.in_common(cand.id) if split is not None else None)
            log.success("[{}/{}] Отклик (сегодня {}): {}  {}",
                        len(applied), eff, total, cand.name, cand.url)
            # Возврат письма НЕ отбрасываем (аудит 22.09.2026, §1): отклик без сопроводительного
            # работодателю — это дефект, и до 22.09 его не было видно нигде, кроме INFO в глубине
            # лога. Формат тот же, что у feed-пути (`_apply_one_vacancy` -> result["letter"]).
            if not _send_cover_via_chat(page, cand, cover.build_cover(cand, cover_mode)):
                letters_failed += 1
        elif status is ApplyOutcome.ALREADY:
            reconciled[cand.id] = VacancyMark.APPLIED.code
            log.info("Уже откликались — синхронизирую marks: {}", cand.name)
        elif status is ApplyOutcome.FORM:
            forms_n += 1               # считаем ИСХОД прогона, а не рост очереди: повторная
            added = store.add_form(cand.id, cand.name, cand.url)   # анкета тоже съела карточку
            log.info("Анкета: {} -> {} {}", cand.name,
                     "в форм-очередь" if added else "уже в форм-очереди", cand.url)
        elif status is ApplyOutcome.CAPTCHA:
            # дальше идти бессмысленно и вредно: каждая следующая карточка — ещё один
            # бот-сигнал. Проверка снимается только человеком, в профиле автоматики.
            log.error("HH показал капчу (/account/captcha) — прогон ОСТАНОВЛЕН на {}. "
                      "Пройди проверку вручную: hh.py autoclick --login", cand.url)
            break
        elif status is ApplyOutcome.UNCONFIRMED:
            # Клик был, подтверждения от HH нет: упор в потолок скользящих суток, либо на нашей
            # стороне не снялся `disabled` сабмит (обязательное письмо, смена вёрстки). Отдельный
            # счётчик нужен ИМЕННО здесь: до 23.09.2026 эти отказы шли под общим skip, и прогон
            # продолжал кликать в стену — 33 вакансии за 18 минут, каждый клик бот-сигнал
            # (порог APPLY_SKIP_STREAK_MAX=50 калиброван под «нет кнопки», то есть под архив).
            skipped += 1
            streak += 1
            unconfirmed += 1
            unconfirmed_streak += 1
            log.info("Пропуск: {}  {}", cand.name, cand.url)   # причину пишет apply_one выше
            if unconfirmed_streak >= APPLY_UNCONFIRMED_STREAK_MAX:
                log.error("{} попыток ПОДРЯД не подтвердились (клик был, ответа HH нет) — прогон "
                          "ОСТАНОВЛЕН. За скользящие {}ч откликов {} (потолок HH ~{}): похоже, "
                          "упёрлись в него; если число далеко от потолка — дело в форме отклика "
                          "(залипший disabled сабмит), причины отказов строками выше. Окно: "
                          "hh.py autoclick --dry-pool; сессия: hh.py autoclick --login",
                          unconfirmed_streak, quota.ROLLING_WINDOW_H,
                          quota.applied_in_window(store.applied_log()), HH_APPLY_ROLLING_CAP)
                break
        else:
            skipped += 1
            streak += 1
            # причину пишет apply_one строкой выше — здесь только сам факт и вакансия
            log.info("Пропуск: {}  {}", cand.name, cand.url)
            if streak >= APPLY_SKIP_STREAK_MAX:
                # Длинная серия пропусков — сигнал, что прогон идёт вхолостую. Дальше идти
                # вредно: если причина в аккаунте, каждая карточка — ещё один бот-сигнал
                # (та же логика, что у капча-гейта выше).
                #
                # ВЕРДИКТ НЕ СТАВИМ. Предохранитель ставился 02.08.2026 с формулировкой
                # «похоже на блокировку HH», и это оказалось неверно: 03.08 разбор показал,
                # что 47 из 50 таких вакансий были живые (`active=true archived=false`), а
                # отклик не уходил из-за ОБЯЗАТЕЛЬНОГО сопроводительного — кнопка submit
                # приходила `disabled` (см. _fill_letter_if_required). Порог 50 был выбран
                # по замеру, где «здоровые» сутки уже содержали этот же дефект, поэтому
                # калибровать его заново надо на чистых прогонах, а не на той статистике.
                #
                # Причину каждого пропуска теперь пишет apply_one — она в логе выше.
                log.error("{} вакансий ПОДРЯД без отклика — прогон идёт вхолостую и "
                          "ОСТАНОВЛЕН. Причины пропусков — строками выше (архив, внешний сайт, "
                          "опросник); серию «клик был, а подтверждения нет» ловит отдельный "
                          "предохранитель APPLY_UNCONFIRMED_STREAK_MAX. Сессию проверить: "
                          "hh.py autoclick --login", streak)
                break
        if status not in (ApplyOutcome.SKIP, ApplyOutcome.UNCONFIRMED):
            streak = 0                 # любой не-пропуск снимает подозрение
            unconfirmed_streak = 0     # ...и серию «клик был, подтверждения нет» тоже:
                                       # ALREADY/FORM доказывают, что поток отклика живой
        time.sleep(random.uniform(*APPLY_PAUSE))

    # найденные «уже откликались» — в marks (чтобы не выбирать их впредь); новые отклики
    # уже отмечены/заквочены по одному внутри цикла.
    if reconciled:
        store.merge_marks(reconciled)
    if applied:
        log.success("Откликов: {} (сегодня {}/{}), синхронизировано ранее откликнутых: {}",
                    len(applied), store.applied_today(), daily_cap, len(reconciled))
    elif reconciled:
        log.info("Новых откликов 0; синхронизировано ранее откликнутых: {}", len(reconciled))
    # Сводка прогона — ЕДИНСТВЕННЫЙ сигнал, по которому деградация видна В ЭКСПЛУАТАЦИИ.
    # Отношение пропусков к откликам росло с 0.3 до 36 за две недели, и заметил это
    # пользователь, а не лог: строк «Пропуск» много, но никто их не считал.
    # Ориентир: скип/отклик <=1.5 — норма, >=5 — разбираться; анкет вдвое больше откликов —
    # тоже разбираться (см. _run_summary).
    level, line = _run_summary(len(applied), skipped, forms_n, len(reconciled), letters_failed,
                               unconfirmed)
    (log.warning if level == "warning" else log.info)(line)
    return len(applied)


@dataclass(frozen=True)
class RunOptions:
    """Параметры одного браузерного прогона (bump + apply) — VO вместо девяти аргументов `run`.

    Собирает CLI (`hh.py::_do_autoclick`); значения по умолчанию — прежние дефолты `run`.
    `do_bump`/`do_apply` разводят слитую задачу на поднятие и отклики (--bump-only/--apply-only),
    `force_bump` обходит кулдаун поднятия по явному намерению пользователя."""
    apply_limit: int | None = None
    daily_cap: int = DAILY_CAP_DEFAULT
    headless: bool = True
    do_bump: bool = True
    do_apply: bool = True
    cover_mode: str = "template"
    lock_retries: int = 6
    lock_wait: float = 60
    force_bump: bool = False


def run(options: RunOptions) -> None:
    """Поднятие резюме И отклики в ОДНОМ браузерном прогоне (bump+apply слиты в одну крон-
    задачу: им нужен один браузер и один профиль, поэтому раздельные задачи только дрались
    за общий lock и лишний раз проходили DDoS-Guard).

    Поднятие гейтится кулдауном (`store.bump_due()`, лимит HH — раз в 4ч): отклики идут
    каждые 90 мин, и без гейта тяжёлая /applicant/resumes грузилась бы впустую на каждом
    слоте. force_bump=True (CLI --bump-only) обходит гейт — явное намерение пользователя.

    Один браузер на профиль (single-instance lock); в кроне ЖДЁМ lock (сервер ленты держит
    тот же профиль ~300с после отклика) — по умолчанию до lock_retries*lock_wait ≈ 6 мин.
    cover_mode: template|llm."""
    from playwright.sync_api import sync_playwright
    with (watchdog._hang_watchdog(),
          lock._single_instance(options.lock_retries, options.lock_wait),
          sync_playwright() as p):
        ctx = browser._launch(p, headless=options.headless)
        page = browser._page(ctx)
        try:
            state = browser._session_state(page)
            if state is browser.LoginState.UNKNOWN:
                # НЕ «сессия протухла»: страница не доехала (DDoS-Guard/сеть/упавший контекст).
                # Автовход паролем и запись EXPIRED тут — ложный сигнал в ленте («нужен вход»)
                # и лишний вход при ЖИВОЙ сессии (аудит 22.09.2026, §1). Останавливаемся тихо.
                raise SystemExit(
                    f"Аккаунт {ACCOUNT.code}: состояние сессии неизвестно (страница HH не доехала) "
                    "— прогон остановлен без автовхода, повтор на следующем слоте")
            if state is browser.LoginState.ANONYMOUS:
                # сессия протухла — в кроне пробуем перелогиниться по паролю сами
                log.info("Сессии нет/протухла — пробую автовход по паролю…")
                if not (browser._auto_login(page)
                        and browser._session_state(page) is browser.LoginState.LOGGED_IN):
                    account_session.record_session(account_session.SessionState.EXPIRED)
                    raise SystemExit(
                        f"Аккаунт {ACCOUNT.code}: сессии нет, автовход не прошёл (капча/код/SMS) — "
                        f"запусти: python hh.py autoclick --login при HR_ACCOUNT={ACCOUNT.code}")
            if not account_session.verify_session():
                raise SystemExit(f"Аккаунт {ACCOUNT.code}: личность сессии не совпала — прогон "
                                 "остановлен до откликов (причина строкой выше)")
            if options.do_bump and (options.force_bump or store.bump_due()):
                # отмечаем ТОЛЬКО реальное поднятие: 0 = кулдаун HH, -1 = страница не
                # отрисовалась -> кулдаун не взводим, попробуем на следующем слоте
                if browser.bump_resumes(page) > 0:
                    store.mark_bumped()
            elif options.do_bump:
                since = store.hours_since_bump()
                log.info("Поднятие резюме: кулдаун (поднимали {:.1f}ч назад, лимит HH раз в {}ч) "
                         "— пропускаю", since or 0.0, bump_state.BUMP_COOLDOWN_H)
            if options.do_apply:
                _sync_applied_from_chats(ctx, page)   # реконсиляция marks (cookie-only)
                _apply_batch(page, options.apply_limit, options.daily_cap, options.cover_mode)
                _drain_pending(page, options.daily_cap, options.cover_mode)  # дожать из ленты (26+)
                _VACANCY_META.clear()   # снимок реестра жил ради батча и дренажа (аудит §5)
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def _apply_one_vacancy(page: Any, vid: str, url: str, cover_text: str,
                       name: str = "", employer: str = "") -> dict[str, Any]:
    """Отклик + письмо для ОДНОЙ вакансии на уже открытой странице (переиспользуемо воркером).
    Пишет marks/quota/форм-очередь.

    В ЖУРНАЛ УХОДЯТ `cand.name` И `cand.employer`, а не сырые аргументы (инцидент 01.08.2026,
    вторая половина — закрытая для forms-пути и оставшаяся открытой для feed-пути). Лента шлёт
    только id/url/название, поэтому раньше каждая запись feed-пути получала `employer=""`, а при
    пустом `name` — ещё и безымянную строку: журнал append-only, задним числом это не чинится.
    Вакансия уходит из выдачи, карточка-призрак в ленте синтезируется из журнала, и без
    работодателя она не находится ни поиском по компании, ни воронкой `funnel.py`.
    Работодатель берётся из аргумента, иначе из кеша `_VACANCY_META` (его наполняет крон-батч)."""
    known_name, known_employer = _VACANCY_META.get(str(vid), ("", ""))
    cand = Candidate(id=vid, name=name or known_name or vid,
                     url=url or f"https://hh.ru/vacancy/{vid}",
                     employer=employer or known_employer)
    if (owner := taken.taken_by_others().get(str(vid))) is not None:
        # RFC-004: лента и очередь ожидания идут мимо отбора. Не клик; для дренажа это
        # «обработано» (не applied и не captcha), запись в очередь не вернётся.
        log.warning("{}: уже занята аккаунтом {} — отклик не отправлен", vid, owner)
        return {"status": "taken", "owner": owner, "letter": False}
    if employer_blocked(cand.employer):
        # RFC-004: блок-лист работодателей обязан действовать на ВСЕХ путях отклика — лента и
        # очередь pending обходят pick_candidates. Не кликаем. Для дренажа skip = «обработано»:
        # запись не вернётся в очередь и не будет пробоваться каждый прогон.
        log.warning("{}: работодатель «{}» в блок-листе — отклик не отправлен", vid, cand.employer)
        return {"status": ApplyOutcome.SKIP.code, "letter": False}
    result = {"status": "error", "letter": False}
    st = apply_one(page, cand, cover_text=cover_text)
    result["status"] = st.code                   # wire-строка (тот же код) для ответа /api/apply
    if st is ApplyOutcome.APPLIED:
        # та же единая точка, что у крон-батча и форм-дренажа (аудит 22.09.2026, §1).
        # `ab` у ленты неизвестен, поэтому передаём его ЯВНО как None: набор полей записи
        # обязан совпадать с крон-путём (расхождение и было находкой — feed-путь не писал ab).
        total = store.commit_applied(vid, name=cand.name, url=cand.url, employer=cand.employer,
                                     via=ApplyChannel.FEED, ab=None)
        log.success("Отклик из ленты: {} (сегодня {})", vid, total)
        if not cand.employer:
            # не смертельно, но карточка-призрак не найдётся по компании — видно в логе
            log.info("{}: работодатель неизвестен — в журнале останется пустым", vid)
        if cover_text:
            result["letter"] = _send_cover_via_chat(page, cand, cover_text)
    elif st is ApplyOutcome.ALREADY:
        store.mark_applied(vid)
    elif st is ApplyOutcome.FORM:
        store.add_form(vid, cand.name, cand.url)   # имя из ленты/кеша — очередь читаема
    return result


def _drain_pending(page: Any, daily_cap: int, cover_mode: str = "template") -> int:
    """Дренаж очереди ожидания (apply_pending.json): вакансии, что лента добавила, пока
    браузер был занят. Владелец браузера (крон после батча / воркер) дожимает их —
    так клик «в фоне» при занятом кроне становится 26-й вакансией. Уважает ОБА потолка HH:
    дневной (`daily_cap`) и скользящие сутки (`HH_APPLY_ROLLING_CAP` — иначе дренаж
    досылал бы клики в стену уже после того, как батч в неё упёрся).
    Возвращает число РЕАЛЬНО отправленных откликов.

    ПОЛИТИКА ВОССТАНОВЛЕНИЯ (аудит 08.08.2026, находка 5). `pop_pending` снимает запись с диска
    ДО обработки, и до этой правки вызов был обёрнут в `contextlib.suppress(Exception)`, результат
    игнорировался, а `n` рос безусловно. Любая НАША ошибка (TypeError в build_cover, смена DOM)
    съедала запись молча и рапортовала «+1 доп. отклик» — систематическая ошибка выела бы всю
    очередь за один прогон, не оставив следа.

    * ВЫПОЛНЕНО ЦЕЛИКОМ — исход дошёл до `_apply_one_vacancy`: applied/already/form/skip. Запись
      обработана, обратно НЕ возвращается. Skip не возвращаем сознательно: архив и внешний сайт
      не станут живыми от повтора, а каждая новая попытка — ещё один бот-сигнал HH.
    * ЧАСТИЧНО — исключение до или во время обработки: запись возвращается в КОНЕЦ очереди
      (`store.requeue_pending`, поля сохраняются целиком), прогон идёт к следующей.
    * ПОВТОР БЕЗОПАСЕН: если отклик успел уйти, а упали мы на журналировании, следующая попытка
      получит от HH ветку ALREADY (кнопка заменена на «Чат») — дубля работодателю не будет.
    * КРУГ НЕ ЗАМКНЁТСЯ: каждый id пробуем не более одного раза за прогон (`seen`); встретили
      возвращённую запись второй раз — очередь прокручена, выходим.
    * КАПЧА — запись возвращаем и дренаж рвём: под капчей следующие клики только вредят.

    НАБЛЮДАЕМОСТЬ: строка «Очередь ленты: обработано N, откликов +K, возвращено R, осталось Q».
    Систематическая поломка = R>0 при неубывающем Q от прогона к прогону."""
    applied = returned = 0        # returned — сколько записей легло обратно в очередь
    # Остаток скользящего окна на входе в дренаж. Именно ОСТАТОК, а не проверка «дошли до
    # потолка»: окно общее у батча и дренажа, и батч мог уже съесть его целиком.
    rolling_room = max(0, HH_APPLY_ROLLING_CAP - quota.applied_in_window(store.applied_log()))
    if rolling_room <= 0:
        log.warning("Очередь ленты НЕ дренажирую: за скользящие {}ч откликов {} (потолок HH ~{})",
                    quota.ROLLING_WINDOW_H, quota.applied_in_window(store.applied_log()),
                    HH_APPLY_ROLLING_CAP)
        return 0
    seen: set[str] = set()
    while store.applied_today() < daily_cap and applied < rolling_room:
        rec = store.pop_pending()
        if not rec:
            break
        vid = str(rec.get("id"))
        if vid in seen:                        # круг замкнулся — вернуть и выйти
            store.requeue_pending(rec)
            break
        seen.add(vid)
        try:
            text = rec.get("cover") or cover.build_cover(
                Candidate(id=vid, name=rec.get("name") or vid, url=""), cover_mode)
            res = _apply_one_vacancy(page, vid, rec.get("url", ""), text,
                                     rec.get("name", ""), rec.get("employer", ""))
        except Exception as e:
            returned += 1
            left = store.requeue_pending(rec)
            log.error("Очередь ленты: {} НЕ обработана ({}: {}) — возвращена в очередь, в ней {}",
                      vid, type(e).__name__, e, left)
            time.sleep(random.uniform(*APPLY_PAUSE))   # упасть могли уже ПОСЛЕ навигации — не долбим HH
            continue
        status = str(res.get("status") or "")
        if status == ApplyOutcome.APPLIED.code:
            applied += 1
        elif status == ApplyOutcome.CAPTCHA.code:
            returned += 1          # в счётчик тоже: строка ниже — сигнал здоровья очереди
            store.requeue_pending(rec)
            log.error("Очередь ленты: HH показал капчу — дренаж ОСТАНОВЛЕН, {} возвращена "
                      "в очередь", vid)
            break
        else:
            log.info("Очередь ленты: {} -> {} (в отклики не зачтено)", vid, status)
        time.sleep(random.uniform(*APPLY_PAUSE))
    if seen:
        log.info("Очередь ленты: обработано {}, откликов +{}, возвращено в очередь {}, "
                 "осталось {}", len(seen), applied, returned, len(store.pending()))
    if applied:
        log.success("Очередь ленты дренажирована: +{} доп. откликов (26+)", applied)
    return applied
