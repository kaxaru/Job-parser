"""Автокликер hh.ru (Playwright): поднятие резюме + автоотклики под своим аккаунтом.

Сессия — persistent-профиль Chromium в data/browser_profile (гитигнорен вместе с data/):
  1) python hh.py autoclick --login   — ОДИН раз, с окном: вход по HH_EMAIL+HH_PASSWORD
     (.env) автоматом; капчу/код (если HH попросит) вводишь руками.
  2) python hh.py autoclick           — headless: поднять резюме + отклики (по крону);
     протухшая сессия самовосстанавливается автовходом по паролю.

Селекторы — data-qa-атрибуты HH и текстовые локаторы Playwright: хэшированные
magritte-классы (напр. magritte-input___LVTID_9-4-29) меняются каждым деплоем HH
и как селекторы непригодны.

Playwright — опциональная зависимость (паттерн psycopg2 у поиска): импорт ленивый,
без него остальные режимы hh.py работают, а pick_candidates() тестируется без браузера.
"""
import contextlib
import datetime
import faulthandler
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from hrwork.application.apply import cover, session
from hrwork.application.apply.candidates import Candidate, pick_candidates
from hrwork.application.apply.chat import chat
from hrwork.application.apply.forms.form_status import skippable_form_ids
from hrwork.application.apply.outcome import ApplyChannel, ApplyOutcome, VacancyMark
from hrwork.application.apply.runtime import bump_state
from hrwork.application.apply.runtime.lock import (
    _single_instance as _single_instance,  # реэкспорт: forms.py берёт его как autoclick._single_instance
)
from hrwork.application.apply.runtime.lock import release_if_mine
from hrwork.application.apply.runtime.quota import DAILY_CAP_DEFAULT
from hrwork.application.apply.runtime.store import store
from hrwork.config import APPLY_SKIP_STREAK_MAX, DATA_DIR, FORMS_ENABLED, log
from hrwork.infrastructure.sources.hh import BROWSER_UA
from hrwork.infrastructure.storage import vacancy_repository

LOGIN_URL   = "https://hh.ru/account/login"
RESUMES_URL = "https://hh.ru/applicant/resumes"
HOME_URL    = "https://hh.ru/"   # лёгкая страница для проверки логина (не тяжёлый /resumes)
PROFILE_DIR = DATA_DIR / "browser_profile"
HH_EMAIL    = os.getenv("HH_EMAIL", "").strip()
HH_PASSWORD = os.getenv("HH_PASSWORD", "").strip()

# Квота откликов и single-instance lock вынесены в quota.py / lock.py (чистая логика).

# data-qa элементов многошаговой формы /account/login (стабильны между деплоями,
# в отличие от хэшированных magritte-классов). Флоу:
#   submit-button (тип APPLICANT по умолчанию) -> credential-type-EMAIL (radio)
#   -> applicant-login-input-email -> expand-login-by-password
#   -> applicant-login-input-password -> submit-button
_SUBMIT       = '[data-qa="submit-button"]'
_CRED_EMAIL   = '[data-qa="credential-type-EMAIL"]'
_EMAIL_INPUT  = '[data-qa="applicant-login-input-email"]'
_PW_TOGGLE    = '[data-qa="expand-login-by-password"]'
_PW_INPUT     = '[data-qa="applicant-login-input-password"]'
_ANON_MARKERS = '[data-qa="login"], [data-qa="account-login-form"]'   # видит только аноним

# ── Отклик: параметры батча (отбор кандидатов -> candidates.pick_candidates) ──
APPLY_LIMIT_DEFAULT = 10                                 # откликов за один запуск
APPLY_PAUSE = (4.0, 9.0)   # пауза между откликами, сек — не долбим HH очередями
POOL_MULT = 5              # пул кандидатов = eff × POOL_MULT (запас на пропуски: опросник/архив/уже)

# ── Watchdog зависаний ──
# ПОДТВЕРЖДЕНО стеком (19.07): виснет `page.wait_for_selector(_HH_ROOT, timeout=20_000)`
# внутри _goto — вызов с 20-секундным таймаутом простоял 50 минут. Причина: таймауты Playwright
# исполняет NODE-драйвер, и когда связка драйвер/CDP клинит, python блокируется в `_sync()` на
# трубе НАВСЕГДА — собственный таймаут Playwright не спасает. Нужен внешний дедлайн.
#
# ExecutionTimeLimit планировщика для этого НЕ годится: он снимает только .bat, а python +
# node + chromium ОСИРОТЕВАЮТ и продолжают держать профиль и autoclick.lock (наблюдали 19.07 —
# после «завершения» задачи жило 10 процессов). Поэтому убиваем дерево сами: taskkill /T по
# собственному pid снимает и node-драйвер, и Chromium.
# Пороги ПРОВЕРЕНЫ и оставлены прежними (замер 03.08.2026 по 128 прогонам): здоровый прогон
# идёт медиана 27 мин, p90 36, максимум 37.4 — снижать 50 минут некуда, любое ужесточение
# начинает резать рабочие прогоны, а выигрыш (45 вместо 50) в пределах шума.
# Дедлайна на ОТДЕЛЬНЫЙ Playwright-вызов не существует: sync-API привязан к своему потоку
# (вызов из чужого валит драйвер с greenlet.error), а залипший вызов отпускает python только
# со смертью процесса. Поэтому watchdog остаётся ЕДИНСТВЕННЫМ средством против зависания,
# а от МОЛЧАЛИВОЙ деградации защищает предохранитель APPLY_SKIP_STREAK_MAX ниже.
WATCHDOG_DUMP_S = 40 * 60   # стек всех потоков в лог — диагностика
WATCHDOG_KILL_S = 50 * 60   # жёсткий снос дерева (до ExecutionTimeLimit=60м, тот уже не нужен)


def _kill_own_tree() -> None:
    """Снести себя вместе с потомками (node-драйвер + Chromium). Вызывается из таймера-демона,
    когда основной поток намертво заблокирован в Playwright и вернуть управление невозможно."""
    log.error("WATCHDOG: прогон завис (>{} мин) — снимаю дерево процессов", WATCHDOG_KILL_S // 60)
    faulthandler.dump_traceback(file=sys.stderr)          # стек ПЕРЕД сносом
    # ИНЦИДЕНТ 25.07.2026: taskkill /T снимает и НАС САМИХ, поэтому `finally` в
    # _single_instance не наступает — lock оставался с мёртвым pid. Отдаём его сами, ДО
    # выстрела, иначе файл переживает прогон и блокирует крон-слоты. Осознанная цена —
    # под-секундное окно, в котором ждущий инстанс может взять lock, пока наш Chromium ещё
    # умирает (docs/errors.md: «Осиротевший lock»); выигрыш — иммунитет к reuse pid.
    if release_if_mine():
        log.info("WATCHDOG: autoclick.lock отдан до сноса дерева")
    with contextlib.suppress(Exception):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())],
                       capture_output=True, timeout=30)
    with contextlib.suppress(Exception):                  # если taskkill не отработал
        os._exit(1)


@contextlib.contextmanager
def _hang_watchdog(dump_s: int = WATCHDOG_DUMP_S,
                   kill_s: int = WATCHDOG_KILL_S) -> Iterator[None]:
    faulthandler.dump_traceback_later(dump_s, exit=False)
    killer = threading.Timer(kill_s, _kill_own_tree)
    killer.daemon = True
    killer.start()
    try:
        yield
    finally:
        killer.cancel()
        faulthandler.cancel_dump_traceback_later()


# ─────────────────────────── браузер (Playwright) ───────────────────────────

def _launch(p: Any, headless: bool) -> Any:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        user_agent=BROWSER_UA,          # headless-UA («HeadlessChrome») HH режет
        locale="ru-RU",
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _page(ctx: Any) -> Any:
    return ctx.pages[0] if ctx.pages else ctx.new_page()


# HH за DDoS-Guard: первая навигация попадает на JS-челлендж (страница «проверяем
# браузер»), который сам редиректит на реальный HH за пару секунд, минтя cookie
# в профиль. Ждём появления React-корня HH; если это интерстишл — он успеет пройти.
_HH_ROOT = "#HH-React-Root"
_DDG_HINT = ("ddos-guard", "проверяем ваш браузер", "checking your browser")


_CAPTCHA_PATH = "/account/captcha"


def is_captcha(page: Any) -> bool:
    """HH увёл на СВОЮ капчу: редирект на `/account/captcha?backurl=…`, картинка
    `[data-qa="account-captcha-picture"]`. Это НЕ DDoS-Guard (тот держит edge и лечится
    ожиданием) — проверка привязана к аккаунту и снимается только человеком.

    Отличать её от архивной вакансии обязательно: страница капчи проходит `_goto` (корень HH
    на ней есть), кнопки отклика на ней нет, и прогон принимал её за «архив/внешний» —
    28.07 так было перемолото 35 карточек за 50 минут и полсотни анкет, причём каждый клик
    подтверждал HH, что перед ним бот."""
    with contextlib.suppress(Exception):
        return _CAPTCHA_PATH in (page.url or "")
    return False


# ── Дедлайн на браузерный вызов: НЕ через поток ─────────────────────────────────────────
# Первая попытка (03.08.2026) гоняла Playwright-вызов в демон-потоке с join(timeout).
# Это НЕПРАВИЛЬНО и ломает драйвер: sync-API Playwright построен на greenlet'ах, привязанных
# к потоку, создавшему соединение, и вызов из чужого потока падает с
#   greenlet.error: cannot switch to a different thread (which happens to have exited)
# Юнит-тесты этого не показали — обёртку проверяли на time.sleep, а не на живой странице.
#
# Рабочий приём тот же, что уже доказан watchdog'ом: залипший вызов не отпускает python
# ничем, кроме СМЕРТИ ПРОЦЕССА, поэтому дедлайн живёт на уровне прогона (WATCHDOG_KILL_S),
# а не отдельного вызова. Снижен с 50 до 8 минут: клин стоит крону 8 минут вместо целого
# слота, а браузер поднимется заново следующим слотом.


def _goto(page: Any, url: str, tries: int = 3) -> bool:
    """Навигация с ожиданием прохождения DDoS-Guard. True — доехали до HH-приложения."""
    for attempt in range(tries):
        with contextlib.suppress(Exception):
            page.goto(url, wait_until="domcontentloaded")
        with contextlib.suppress(Exception):
            page.wait_for_selector(_HH_ROOT, timeout=20_000)
            page.wait_for_timeout(800)
            return True
        # челлендж не уступил — дать ему дожевать JS и повторить. Чтение title/body
        # обёрнуто: во время навигации DDoS-Guard контекст рушится («Execution context
        # was destroyed») и голый page.title() уронил бы весь прогон.
        title = body = ""
        with contextlib.suppress(Exception):
            title = (page.title() or "").lower()
        with contextlib.suppress(Exception):
            body = (page.inner_text("body")[:200] or "").lower()
        is_ddg = any(h in title or h in body for h in _DDG_HINT)
        if is_ddg:
            log.info("DDoS-Guard: жду прохождения челленджа (попытка {}/{})…", attempt + 1, tries)
        with contextlib.suppress(Exception):
            page.wait_for_timeout(6_000 if is_ddg else 2_000)
    return False


def _logged_in(page: Any) -> bool:
    """Логин-статус по ЛЁГКОЙ главной (не тяжёлый /applicant/resumes — тот грузит весь
    список резюме и висит). Детект по маркерам анонима в шапке: кнопка «Войти»
    (data-qa=login) / форма логина (account-login-form)."""
    _goto(page, HOME_URL)                       # лёгкая страница + переждать DDoS-Guard
    with contextlib.suppress(Exception):        # дождаться отрисовки шапки (SPA)
        page.wait_for_selector('[data-qa^="mainmenu"]', timeout=10_000)
    page.wait_for_timeout(1_500)                # дать осесть возможной навигации
    if "/account/login" in page.url or "/account/signup" in page.url:
        return False
    ok = False
    try:
        ok = page.locator(_ANON_MARKERS).count() == 0
    except Exception:                           # навигация в момент count -> перепроверим
        page.wait_for_timeout(1_500)
        with contextlib.suppress(Exception):
            ok = page.locator(_ANON_MARKERS).count() == 0
    if ok:
        # Живая сессия -> выгружаем куки для HTTP-синка (он работает без браузера и без lock).
        session.save_state(page.context)
    return ok


def _shown(page: Any, selector: str) -> bool:
    """Виден ли (в DOM и visible) хоть один элемент по селектору."""
    with contextlib.suppress(Exception):
        loc = page.locator(selector).first
        return bool(loc.count() > 0 and loc.is_visible())
    return False


def _try_click(page: Any, selector: str, *, force: bool = False) -> bool:
    with contextlib.suppress(Exception):
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=6_000)
        loc.check(force=True) if force else loc.click(timeout=6_000)
        return True
    return False


def _try_fill(page: Any, selector: str, value: str) -> bool:
    with contextlib.suppress(Exception):
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=6_000)
        loc.fill(value, timeout=6_000)
        return True
    return False


def _on_login_form(page: Any) -> bool:
    """Ещё на форме входа (не залогинились)."""
    with contextlib.suppress(Exception):
        if "/account/login" in page.url:
            return True
        return bool(page.locator('[data-qa="account-login-form"]').count() > 0)
    return True


def _auto_login(page: Any) -> bool:
    """Автовход по HH_EMAIL(+HH_PASSWORD) — АДАПТИВНЫЙ автомат: на каждой итерации
    смотрит, какой элемент формы сейчас на экране, и делает один шаг. Устойчив к
    перерисовкам DDoS-Guard и к порядку появления полей (в отличие от жёсткой
    последовательности кликов). Форма /account/login многошаговая:
    тип аккаунта -> способ входа (e-mail) -> e-mail -> «войти по паролю» -> пароль -> войти.

    True — вошли; False — застряли (капча / нет пароля / изменилась вёрстка)."""
    if not HH_EMAIL:
        log.info("HH_EMAIL не задан в .env — вводи e-mail руками")
        return False
    if not _goto(page, LOGIN_URL):
        log.warning("DDoS-Guard не пройден на странице логина — headed/повтори")

    filled_email = filled_pw = False
    for step in range(14):                     # ограничитель: форма ≤6 шагов + запас на DDG
        page.wait_for_timeout(1_200)           # дать SPA/челленджу дорисоваться
        if not _on_login_form(page):
            return True                        # форма исчезла -> вошли

        # 1) поле пароля показано -> вводим пароль и жмём войти
        if (HH_PASSWORD and not filled_pw and _shown(page, _PW_INPUT)
                and _try_fill(page, _PW_INPUT, HH_PASSWORD)):
            filled_pw = True
            log.debug("autologin[{}]: пароль введён -> войти", step)
            _try_click(page, _SUBMIT)
            continue

        # 2) поле e-mail показано -> вводим e-mail. С паролем submit НЕ жмём (он ушлёт
        #    код на почту) — раскрытие пароля делает ветка 4 на следующей итерации.
        if (not filled_email and _shown(page, _EMAIL_INPUT)
                and _try_fill(page, _EMAIL_INPUT, HH_EMAIL)):
            filled_email = True
            log.debug("autologin[{}]: e-mail введён", step)
            if HH_PASSWORD:
                _try_click(page, _PW_TOGGLE)   # «Войти по паролю» (если уже виден)
            else:
                _try_click(page, _SUBMIT)      # без пароля -> код на почту (ожидаемо)
            continue

        # 3) выбор способа входа: переключаемся на e-mail (radio скрыт -> force)
        if not filled_email and _shown(page, _CRED_EMAIL):
            _try_click(page, _CRED_EMAIL, force=True)
            continue

        # 4) e-mail введён, пароль ещё не показан -> жмём «Войти по паролю» и ЖДЁМ поле.
        #    submit тут НЕ жмём намеренно: на этом шаге он отправляет код на почту.
        if filled_email and HH_PASSWORD and not filled_pw:
            _try_click(page, _PW_TOGGLE)       # no-op, если тоггла ещё нет — ждём в цикле
            continue

        # 5) промежуточный шаг ДО ввода e-mail (выбор типа аккаунта) -> «Дальше».
        #    После e-mail с паролем сюда не попадаем (ветка 4 делает continue) —
        #    иначе submit ушлёт код вместо входа по паролю.
        if not filled_email and _try_click(page, _SUBMIT):
            log.debug("autologin[{}]: submit (шаг до e-mail)", step)
            continue

        break                                  # ничего знакомого — застряли (капча?)

    ok = not _on_login_form(page)
    if not ok:
        log.warning("Автовход не завершился (капча/код/вёрстка) — доделай в окне (--login)")
    return ok


_CAPTCHA = '[data-qa="account-captcha-input"], [data-qa*="captcha"]'


def _captcha_shown(page: Any) -> bool:
    with contextlib.suppress(Exception):
        return bool(page.locator(_CAPTCHA).count() > 0)
    return False


def login() -> None:
    """Разовый вход с окном: автовход по e-mail+паролю заполняет ВСЮ форму; если HH
    показал капчу — её проходишь руками (её и только её), сессия остаётся в PROFILE_DIR
    и дальше run() работает headless по крону без логина, пока сессия не истечёт."""
    from playwright.sync_api import sync_playwright
    with _single_instance(), sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = _page(ctx)
        try:
            if _logged_in(page):
                log.success("Уже залогинен — профиль: {}", PROFILE_DIR)
                return
            if _auto_login(page) and _logged_in(page):
                log.success("Автовход выполнен — сессия сохранена: {}", PROFILE_DIR)
                return
            if _captcha_shown(page):
                log.warning("HH показал КАПЧУ — пройди её в окне (e-mail+пароль уже введены). "
                            "После капчи, если надо, нажми «Войти». НЕ закрывай окно.")
            else:
                log.info("Доверши вход в окне (код из письма/DDoS-Guard). НЕ закрывай окно.")
            log.info("Жду до 5 минут и закрою окно сам после проверки…")
            # пока форма не исчезнет: как только капча пройдена и мы на экране ПАРОЛЯ —
            # дожимаем «Войти». submit жмём ТОЛЬКО при видимом поле пароля, чтобы
            # случайно не уйти в отправку кода на почту.
            for _ in range(150):               # 150 × 2c = 5 мин
                if not _on_login_form(page):
                    break
                if not _captcha_shown(page) and _shown(page, _PW_INPUT):
                    _try_click(page, _SUBMIT)
                page.wait_for_timeout(2_000)
            if _logged_in(page):
                log.success("Вход сохранён: {} — теперь run() идёт headless по крону", PROFILE_DIR)
            else:
                log.error("Вход не подтвердился — повтори: python hh.py autoclick --login")
        finally:
            with contextlib.suppress(Exception):    # окно могли закрыть руками
                ctx.close()


def bump_resumes(page: Any) -> int:
    """Клик БЕСПЛАТНОГО «Поднять в поиске» по всем резюме. HH разрешает раз в 4 часа —
    после поднятия кнопка ПРОПАДАЕТ (не disabled!) и заменяется текстом «Поднять
    вручную можно сегодня в HH:MM». Это норма, не ошибка.

    ВАЖНО про селектор: у доступной бесплатной кнопки data-qa СОСТАВНОЙ —
    "resume-update-button resume-update-button_actions" (два токена через пробел),
    текст «Поднять в поиске». На кулдауне остаётся только "resume-update-button_actions"
    с текстом «Поднять автоматически» (платно). Поэтому матчим по ТОКЕНУ (~=), а не
    точным совпадением, и дополнительно гардим по тексту «в поиске» — платную не трогаем.

    Возвращает число поднятых. -1 = страница резюме НЕ отрисовалась (DDoS-Guard/headless
    отдал деградированную страницу) — это НЕ кулдаун, а повод повторить/уйти в headed."""
    _goto(page, RESUMES_URL)
    # Дождаться реальной отрисовки списка резюме (SPA грузит карточки async; на headless
    # после DDoS-Guard список может не догрузиться — тогда кнопки «нет» ложно).
    rendered = False
    for _ in range(4):
        with contextlib.suppress(Exception):
            page.wait_for_selector(
                '[data-qa^="resume-card"], button[data-qa~="resume-update-button"]',
                timeout=8_000)
            rendered = True
            break
        page.wait_for_timeout(2_000)
    if not rendered:
        log.warning("Страница резюме не отрисовалась (DDoS-Guard/headless?) — "
                    "поднятие пропущено, повтори или запусти с --headed")
        return -1

    btns = page.locator('button[data-qa~="resume-update-button"]')   # токен, не точное
    n = 0
    for i in range(btns.count()):
        try:
            b = btns.nth(i)
            txt = ""
            with contextlib.suppress(Exception):
                txt = b.inner_text(timeout=2_000).lower()
            if "в поиске" not in txt:            # платную «Поднять автоматически» пропускаем
                continue
            if b.is_enabled():
                b.click()
                page.wait_for_timeout(1_500)
                n += 1
        except Exception as e:
            log.debug("Поднятие #{}: {}", i, e)
    if n == 0:
        # кнопки нет, но страница отрисована -> реальный кулдаун; вытащим «...можно в HH:MM»
        when = ""
        with contextlib.suppress(Exception):
            loc = page.get_by_text(re.compile("Поднять вручную можно")).first
            if loc.count():
                when = " — " + loc.inner_text(timeout=2_000).replace("\n", " ").strip()
        log.info("Поднято резюме: 0 (кулдаун HH 4ч, кнопка скрыта{})", when)
    else:
        log.success("Поднято резюме: {} (лимит HH — раз в 4 часа)", n)
    return n


# Кнопка подтверждения отклика в поп-апе (единый CSS-селектор через запятую, НЕ кортеж!
# перебор строки символами кликал бы locator('a')=cookie-ссылку -> лавина вкладок).
# Письмо НЕ пишем в модалку (её поле капризно/не всегда есть) — после отклика пишем в
# СЛОТ сопроводительного через chatik /save (chat.save_cover, cookie-only, надёжно).
_RESPONSE_SUBMIT = ('[data-qa="vacancy-response-letter-submit"], '
                    '[data-qa="vacancy-response-submit-popup"]')
# Поле сопроводительного на странице/в модалке отклика.
_RESPONSE_LETTER = '[data-qa="vacancy-response-popup-form-letter-input"]'
# Маркеры АРХИВНОЙ вакансии (одна CSS-строка с запятой, не кортеж — см. _RESPONSE_SUBMIT).
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
        btn = page.locator(_RESPONSE_SUBMIT).first
        if not btn.count():
            bits.append("сабмит=нет")
        else:
            bits.append("сабмит=disabled" if btn.is_disabled(timeout=1_000) else "сабмит=активен")
    with contextlib.suppress(Exception):
        letter = page.locator(_RESPONSE_LETTER).first
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
    поэтому режим 'llm' не тратит запрос на архив и пропуски."""
    with contextlib.suppress(Exception):
        btn = page.locator(_RESPONSE_SUBMIT).first
        if not btn.count() or not btn.is_disabled(timeout=1_000):
            return False                        # кнопка активна — письмо не требуется
        letter = page.locator(_RESPONSE_LETTER).first
        if not letter.count():
            return False                        # disabled не из-за письма — не наш случай
        # пробельный текст — не письмо: он не снимет disabled, но затрёт поле
        text = (cover_text or "").strip() or cover.build_cover(cand, cover_mode).strip()
        if not text:
            return False
        letter.fill(text, timeout=3_000)
        log.info("{}: письмо обязательно — вписал в форму отклика ({} симв.)", cand.id, len(text))
        return True
    return False


def apply_one(page: Any, cand: Candidate, cover_text: str = "",
              cover_mode: str = "template") -> ApplyOutcome:
    """Отклик на одну вакансию (письмо — только если HH требует его для отправки). Исход:
      APPLIED — отклик отправлен; ALREADY — уже откликались (кнопка заменена на «Чат»);
      FORM — вакансия с вопросами работодателя (в форм-очередь, руками);
      CAPTCHA — HH увёл на проверку, дальше идти бессмысленно;
      SKIP — архив/внешний сайт/не подтвердилось."""
    _goto(page, cand.url)
    if is_captcha(page):
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
        log.debug("{}: отклик с опросником — в форм-очередь", cand.id)
        return ApplyOutcome.FORM
    btn.click()
    # модалка «вакансия в другом городе/стране»
    with contextlib.suppress(Exception):
        page.locator('[data-qa="relocation-warning-confirm"]').first.click(timeout=3_000)
    # клик мог увести на форму отклика с ВОПРОСАМИ работодателя (/applicant/vacancy_response,
    # data-qa=task-question). При FORMS_LLM=1 — заполняем анкету inline (часть отклика);
    # иначе / при пробеле извлечения — в форм-очередь (человек), не ждём 10с submit.
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
            page.locator(_RESPONSE_SUBMIT).first.click(timeout=3_000)
    ok = page.locator('[data-qa="vacancy-response-link-view-topic"]').or_(
        page.get_by_text("Вы откликнулись")).first
    try:
        ok.wait_for(timeout=10_000)
        return ApplyOutcome.APPLIED
    except Exception:
        log.warning("{}: отклик НЕ подтверждён за 10с — {}", cand.id, _submit_diag(page))
        return ApplyOutcome.SKIP


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


def _journal_name(data: dict[str, Any], vid: str, name_map: dict[str, Any]) -> str:
    """Имя вакансии для журнала: из локального сбора, иначе из resources чата, иначе id."""
    nm: str | None = name_map.get(vid)
    if nm:
        return nm
    vac = ((data.get("resources") or {}).get("vacancies") or {}).get(vid) or {}
    nm_res: str = vac.get("name") or ""
    return nm_res


SYNC_FRESH_DAYS = 7          # сообщения старше -> чат считается устоявшимся


def _sync_from_cache(c: dict[str, Any], cached_msgs: dict[str, Any], cached_statuses: dict[str, Any],
                     journaled: set[str], now: Any) -> bool:
    """True -> чат можно НЕ качать: он в кеше С ПЕРЕПИСКОЙ, статус ТЕРМИНАЛЬНЫЙ и сообщений
    не было SYNC_FRESH_DAYS. Решение по lastMessageTime из СПИСКА чатов (не по кешу): новое
    сообщение в старом чате обновляет метку, и чат синкается снова. Нетерминальные
    (RESPONSE/INTERVIEW/…) качаем всегда — их статус может флипнуться МОЛЧА, без
    сообщения (~20 % отказов приходят без письма в чат). lastActivityTime для отсечки
    непригоден: его обновляет само наше чтение chat_data (см. chat.py::list_chats).
    Пустая кешевая переписка (артефакт неудачного фетча) — не повод скипать: иначе она
    замораживается навсегда (fix.md №5). Любое сомнение -> False, качаем."""
    vid = str(c["vacancyId"])
    if (not (cached_msgs.get(vid) or {}).get("messages") or vid not in journaled
            or cached_statuses.get(vid) not in chat.TERMINAL_STATES):
        return False
    try:
        last = datetime.datetime.fromisoformat(c.get("lastMessageTime") or "")
        return bool((now - last) >= datetime.timedelta(days=SYNC_FRESH_DAYS))
    except (ValueError, TypeError):      # TypeError: naive vs aware — недоверие -> синк
        return False


def sync_statuses(headless: bool = True, limit: int | None = None, full: bool = False) -> dict[str, Any]:
    """Синхронизация из чатов — БЕЗ БРАУЗЕРА (cookie-only HTTP, без fingerprint).
    Один chat_data на чат даёт СРАЗУ:
      1) статус отклика (currentApplicantState) -> response_status.json;
      2) дожурналивание НОВЫХ откликов в applied_log.jsonl с реальной датой (creationTime
         сообщения-отклика) — в т.ч. сделанных РУКАМИ на hh.ru (любой отклик = чат).

    По умолчанию ИНКРЕМЕНТ: терминальные чаты без сообщений SYNC_FRESH_DAYS, уже лежащие
    в кеше, не перекачиваются (полный прогон ~1300 чатов занимал ~20 мин). full=True
    (--sync-full) — качать всё. Запись в обоих режимах — MERGE поверх кеша, не replace:
    сетевая ошибка одного чата или limit не должны стирать ранее известное (fix.md №4).

    Работает поверх состояния сессии (data/hh_state.json), которое сохраняет любой браузерный
    прогон. Chromium НЕ поднимается и `autoclick.lock` НЕ берётся — синк независим от откликов
    и не встаёт вместе с ними (раньше зависший браузер морозил статусы неделями).
    `headless` не используется (браузера нет) — параметр сохранён ради совместимости с CLI.
    limit — потолок чатов. Возвращает карту {vacancyId: state}."""
    req, xsrf = session.open_client()
    if req is None:
        log.error("Нет сохранённой сессии ({}) — запусти браузерный прогон "
                  "(python hh.py autoclick --login), он её сохранит.", session.STATE_FILE)
        return {}
    repo = vacancy_repository().load()
    name_map = {rec.id: rec.vacancy.name for rec in repo}
    employer_map = {rec.id: (rec.vacancy.employer or "") for rec in repo}
    seen = store.applied_ids()
    added = failed = 0
    with req:
        # потолок страниц — от числа известных откликов (по 20 чатов на страницу): один раз
        # магический дефолт 30 уже резал корпус на ~600 при 1287 реальных (fix.md №10)
        pages = max(120, len(seen) // 20 + 10)
        chats = chat.list_chats(req, xsrf, pages=pages)
        if not chats:
            log.warning("Чаты не получены (сессия от {} могла протухнуть) — обновит "
                        "следующий браузерный прогон.", session.state_age_hint())
            return {}
        if limit:
            chats = chats[:limit]
        cached_msgs = store.chat_messages()
        cached_statuses = store.statuses()
        now = datetime.datetime.now(datetime.timezone.utc)
        from_cache = 0
        log.info("Синк из чатов (HTTP, без браузера): {} — статусы + журнал + переписка…",
                 len(chats))
        # merge-база = кеш: прогон обновляет поверх, пропуски/сбои не стирают известное
        msgs_out: dict[str, dict[str, Any]] = dict(cached_msgs)
        statuses: dict[str, str] = dict(cached_statuses)
        for i, c in enumerate(chats, 1):
            vid = str(c["vacancyId"])
            if not full and _sync_from_cache(c, cached_msgs, cached_statuses, seen, now):
                from_cache += 1                    # база уже содержит кешевые записи
                continue                           # без сети и без sleep
            data = chat.chat_data(req, xsrf, c["chatId"], c["applicantId"])
            if not data:                           # сетевая ошибка — кешевую запись не затираем
                failed += 1
                continue
            # переписку сохраняем ЗДЕСЬ: chat_data уже получен, отдельных запросов не нужно
            ch = data.get("chat") or {}
            items = ((ch.get("messages") or {}).get("items")) or []
            msgs_out[vid] = {
                "chatId": c["chatId"],
                # можно ли вообще писать в чат (ENABLED_* / DISABLED_*) — проверяем ДО отправки
                "write": ch.get("writePossibility") or {},
                # type != SIMPLE — служебные («рекрутер присоединился»), в переписку не идут
                "messages": [{"text": m.get("text") or "", "mine": bool(m.get("canEdit")),
                              "ts": m.get("creationTime") or "",
                              "bot": bool((m.get("participantDisplay") or {}).get("isBot"))}
                             for m in items
                             if (m.get("text") or "").strip() and m.get("type") == "SIMPLE"],
            }
            st = chat.deep_get(data, "currentApplicantState")
            if st:
                statuses[vid] = st
            if vid not in seen:                    # новый отклик -> в журнал с реальной датой
                store.log_applied(vid, _journal_name(data, vid, name_map),
                                  f"https://hh.ru/vacancy/{vid}", via=ApplyChannel.HH,
                                  ts=chat.response_time(data),
                                  employer=employer_map.get(vid, ""))
                seen.add(vid)
                added += 1
            if i % 50 == 0:
                log.info("  …{}/{}", i, len(chats))
            time.sleep(random.uniform(0.1, 0.3))   # мягко, но быстрее браузерного пути
    if failed:
        log.warning("Синк: {} чатов не получены (сетевые сбои) — остались кешевыми", failed)
    counts: dict[str, int] = {}
    for st in statuses.values():
        counts[st] = counts.get(st, 0) + 1
    store.save_statuses(statuses)
    store.save_chat_messages(msgs_out)          # переписка -> лента подсветит «ждёт ответа»
    # Чат на вакансии = отклик БЫЛ. Отмечаем это в marks, иначе pick_candidates выбирает их
    # снова, а бот тратит ~20с на загрузку страницы, чтобы узнать «уже откликались»
    # (19.07: синк дожурналировал 392 отклика, и прогон буксовал на реконсиляции).
    known = store.marks()
    fresh = {str(c["vacancyId"]): VacancyMark.APPLIED.code for c in chats
             if str(c["vacancyId"]) not in known}
    if fresh:
        store.merge_marks(fresh)
        log.info("Синк: отмечено в marks как откликнутые: +{} (в marks стало {})",
                 len(fresh), len(known) + len(fresh))
    log.success("Синк: статусов {}, новых в журнал {}, из кеша (старше {} дн) {} -> {}",
                len(statuses), added, SYNC_FRESH_DAYS, from_cache,
                {chat.STATE_LABELS.get(k, k): v for k, v in counts.items()})
    return statuses


def _apply_batch(page: Any, apply_limit: int | None, daily_cap: int,
                 cover_mode: str = "template") -> int:
    """Разослать отклики с сопроводительным письмом, с учётом ДНЕВНОГО лимита HH
    (~200/сутки). Эффективный лимит запуска = min(apply_limit, дневной_остаток) — так
    N мелких запусков за день суммарно не превышают cap (идемпотентно: счётчик в
    apply_quota.json, дубли режет marks.json). Письмо: шаблон или LLM (cover_mode)."""
    used = store.applied_today()
    remaining = max(0, daily_cap - used)
    eff = min(apply_limit or APPLY_LIMIT_DEFAULT, remaining)
    if eff <= 0:
        log.info("Дневной лимит откликов исчерпан: {}/{} — пропускаю отклики", used, daily_cap)
        return 0

    # eff — целевое число УСПЕШНЫХ откликов; пропуски (внешний/уже-откликнутые) его НЕ тратят.
    # Берём пул с запасом (×POOL_MULT) и идём по нему, пока не наберём eff.
    # Вакансии-опросники исключаем ПО ФОРМ-ОЧЕРЕДИ (не через marks): бот их не заполняет,
    # а без исключения очередь упиралась в них каждый прогон.
    # Пропускаем не всю форм-очередь, а только те анкеты, что ещё имеют смысл пропускать:
    # мёртвая форма означает снятую вакансию, но если её ПЕРЕОТКРЫЛИ после свипа — форма
    # могла ожить, и вакансия возвращается в оборот сама (form_status.skippable_form_ids).
    records = vacancy_repository().load()
    published = {r.id: r.vacancy.published_at for r in records}
    forms = skippable_form_ids(store.forms(), store.form_cache(), published)
    pool = pick_candidates(records, store.marks(), eff * POOL_MULT, form_ids=forms)
    log.info("Пул кандидатов: {} (цель {} новых откликов, дневной остаток {}/{}, "
             "опросников пропущено: {}, письмо: {})",
             len(pool), eff, remaining, daily_cap, len(forms), cover_mode)

    applied: dict[str, str] = {}       # НОВЫЕ отклики (идут в квоту)
    reconciled: dict[str, str] = {}    # уже откликались ранее (только синхронизация marks)
    skipped = 0                        # всего пропусков за прогон (для сводки в конце)
    streak = 0                         # ПОДРЯД идущих пропусков — детектор блокировки
    for cand in pool:
        if len(applied) >= eff:        # набрали нужное число НОВЫХ откликов — стоп
            break
        status = ApplyOutcome.SKIP
        try:
            status = apply_one(page, cand, cover_mode=cover_mode)
        except Exception as e:
            log.warning("{} ({}): {}", cand.name, cand.id, e)
        if status is ApplyOutcome.APPLIED:
            applied[cand.id] = VacancyMark.APPLIED.code
            # marks/квоту пишем СРАЗУ (как feed-путь _apply_one_vacancy), а не в конце батча:
            # зависание/kill посреди прогона раньше ТЕРЯЛ отметки всех успешных откликов
            # (18.07: 21 реальный отклик ушёл, marks/квота — нет; спасал лишь чат-синк).
            store.mark_applied(cand.id)
            total = store.bump_quota(1)
            log.success("[{}/{}] Отклик (сегодня {}): {}  {}",
                        len(applied), eff, total, cand.name, cand.url)
            store.log_applied(cand.id, cand.name, cand.url, via=ApplyChannel.CRON,
                              employer=cand.employer)
            _send_cover_via_chat(page, cand, cover.build_cover(cand, cover_mode))
        elif status is ApplyOutcome.ALREADY:
            reconciled[cand.id] = VacancyMark.APPLIED.code
            log.info("Уже откликались — синхронизирую marks: {}", cand.name)
        elif status is ApplyOutcome.FORM:
            store.add_form(cand.id, cand.name, cand.url)
        elif status is ApplyOutcome.CAPTCHA:
            # дальше идти бессмысленно и вредно: каждая следующая карточка — ещё один
            # бот-сигнал. Проверка снимается только человеком, в профиле автоматики.
            log.error("HH показал капчу (/account/captcha) — прогон ОСТАНОВЛЕН на {}. "
                      "Пройди проверку вручную: hh.py autoclick --login", cand.url)
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
                          "ОСТАНОВЛЕН. Причины пропусков — строками выше; если там «письмо "
                          "обязательно» или «НЕ подтверждён», дело в форме отклика, а не в "
                          "сессии. Сессию проверить: hh.py autoclick --login", streak)
                break
        if status is not ApplyOutcome.SKIP:
            streak = 0                 # любой не-пропуск снимает подозрение
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
    # Ориентир: <=1.5 — норма, >=5 — разбираться.
    ratio = f"{skipped / len(applied):.1f}" if applied else "все"
    level = log.warning if (not applied or skipped / max(len(applied), 1) >= 5) else log.info
    level("Итог прогона: откликов {}, пропусков {} (скип/отклик {}), уже откликались {}",
          len(applied), skipped, ratio, len(reconciled))
    return len(applied)


def run(apply_limit: int | None = None, daily_cap: int = DAILY_CAP_DEFAULT,
        headless: bool = True, do_bump: bool = True, do_apply: bool = True,
        cover_mode: str = "template", lock_retries: int = 6, lock_wait: float = 60,
        force_bump: bool = False) -> None:
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
    with _hang_watchdog(), _single_instance(lock_retries, lock_wait), sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        page = _page(ctx)
        try:
            if not _logged_in(page):
                # сессия протухла — в кроне пробуем перелогиниться по паролю сами
                log.info("Сессии нет/протухла — пробую автовход по паролю…")
                if not (_auto_login(page) and _logged_in(page)):
                    raise SystemExit(
                        "Автовход не прошёл (капча/код?) — запусти: python hh.py autoclick --login")
            if do_bump and (force_bump or store.bump_due()):
                # отмечаем ТОЛЬКО реальное поднятие: 0 = кулдаун HH, -1 = страница не
                # отрисовалась -> кулдаун не взводим, попробуем на следующем слоте
                if bump_resumes(page) > 0:
                    store.mark_bumped()
            elif do_bump:
                since = store.hours_since_bump()
                log.info("Поднятие резюме: кулдаун (поднимали {:.1f}ч назад, лимит HH раз в {}ч) "
                         "— пропускаю", since or 0.0, bump_state.BUMP_COOLDOWN_H)
            if do_apply:
                _sync_applied_from_chats(ctx, page)   # реконсиляция marks (cookie-only)
                _apply_batch(page, apply_limit, daily_cap, cover_mode)
                _drain_pending(page, daily_cap, cover_mode)  # дожать добавленные из ленты (26+)
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def _apply_one_vacancy(page: Any, vid: str, url: str, cover_text: str,
                       name: str = "") -> dict[str, Any]:
    """Отклик + письмо для ОДНОЙ вакансии на уже открытой странице (переиспользуемо
    воркером и разовым apply_vacancy). Пишет marks/quota/форм-очередь."""
    cand = Candidate(id=vid, name=name or vid, url=url or f"https://hh.ru/vacancy/{vid}")
    result = {"status": "error", "letter": False}
    st = apply_one(page, cand, cover_text=cover_text)
    result["status"] = st.code                   # wire-строка (тот же код) для ответа /api/apply
    if st is ApplyOutcome.APPLIED:
        store.mark_applied(vid)
        total = store.bump_quota(1)
        log.success("Отклик из ленты: {} (сегодня {})", vid, total)
        store.log_applied(vid, name, cand.url, via=ApplyChannel.FEED,
                          employer=cand.employer)
        if cover_text:
            result["letter"] = _send_cover_via_chat(page, cand, cover_text)
    elif st is ApplyOutcome.ALREADY:
        store.mark_applied(vid)
    elif st is ApplyOutcome.FORM:
        store.add_form(vid, name, cand.url)      # name из ленты — очередь читаема
    return result


def _drain_pending(page: Any, daily_cap: int, cover_mode: str = "template") -> int:
    """Дренаж очереди ожидания (apply_pending.json): вакансии, что лента добавила, пока
    браузер был занят. Владелец браузера (крон после батча / воркер) дожимает их —
    так клик «в фоне» при занятом кроне становится 26-й вакансией. Уважает дневной лимит."""
    n = 0
    while store.applied_today() < daily_cap:
        rec = store.pop_pending()
        if not rec:
            break
        vid = str(rec.get("id"))
        text = rec.get("cover") or cover.build_cover(
            Candidate(id=vid, name=rec.get("name") or vid, url=""), cover_mode)
        with contextlib.suppress(Exception):
            _apply_one_vacancy(page, vid, rec.get("url", ""), text, rec.get("name", ""))
        n += 1
        time.sleep(random.uniform(*APPLY_PAUSE))
    if n:
        log.success("Очередь ленты дренажирована: +{} доп. откликов (26+)", n)
    return n


def apply_vacancy(vacancy_id: Any, url: str = "", cover_text: str = "", headless: bool = True,
                  name: str = "") -> dict[str, Any]:
    """Разовый отклик: поднимает свой Playwright на один вызов и закрывает. Для CLI/тестов;
    сервер использует тёплый ApplyWorker (переиспользует браузер между кликами)."""
    from playwright.sync_api import sync_playwright
    vid = str(vacancy_id)
    try:
        cm = _single_instance()
    except SystemExit:
        return {"status": "busy", "letter": False}
    with cm, sync_playwright() as p:
        ctx = _launch(p, headless=headless)
        page = _page(ctx)
        try:
            if not _logged_in(page) and not (_auto_login(page) and _logged_in(page)):
                return {"status": "no-session", "letter": False}
            return _apply_one_vacancy(page, vid, url, cover_text, name)
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


# ─────── Тёплый воркер откликов для сервера (один браузер на серию кликов) ───────
# Проблема наивного /api/apply: каждый клик поднимал НОВЫЙ браузер (старт + DDoS-Guard
# + логин) и закрывал — медленно и расточительно. Воркер держит ОДИН браузер в выделенном
# потоке (Playwright sync НЕ потокобезопасен — работаем из одного потока), переиспускает
# его между кликами, а по простою IDLE_TIMEOUT закрывает и отпускает lock (чтобы не
# блокировать крон). Запросы шлют задания в очередь и ждут результат.

def _start_playwright() -> Any:
    """Запуск Playwright, вынесен для тестируемости (мокается в тестах воркера)."""
    from playwright.sync_api import sync_playwright
    return sync_playwright().start()


class ApplyWorker:
    IDLE_TIMEOUT = 300   # сек без кликов -> закрыть браузер, отпустить lock (пустить крон)
    RESULT_TIMEOUT = 600  # предохранитель: submit не виснет вечно, даже если поток умер молча

    def __init__(self, headless: bool = True):
        self._q: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._headless = headless

    def submit(self, vid: str, url: str, cover: str, name: str = "") -> dict[str, Any]:
        """Поставить отклик в очередь и дождаться результата (блокирует поток запроса)."""
        done = threading.Event()
        box: dict[str, Any] = {}
        # Кладём в очередь ДО старта потока: иначе поток мог бы упасть и слить пустую очередь
        # раньше, чем задание попадёт в неё (гонка -> вечное ожидание). finally потока сольёт
        # это задание статусом 'error', SystemExit-ветка — 'busy'.
        self._q.put((str(vid), url, cover, name, done, box))
        self._ensure_thread()
        if not done.wait(timeout=self.RESULT_TIMEOUT):
            log.error("ApplyWorker: результат не пришёл за {}с — таймаут", self.RESULT_TIMEOUT)
            return {"status": "error", "letter": False, "error": "timeout"}
        res: dict[str, Any] = box.get("result", {"status": "error", "letter": False})
        return res

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        try:
            # короткий ретрай: транзиентный lock (быстрая задача) успеет освободиться;
            # длинный крон-батч (~30 мин) не переждать в HTTP-запросе -> честный busy.
            cm = _single_instance(wait_retries=2, wait_s=15)
            cm.__enter__()
        except SystemExit:
            self._drain("busy")           # lock занят (крон делает отклики) -> задачам busy
            return
        p = ctx = None
        try:
            p = _start_playwright()
            ctx = _launch(p, headless=self._headless)
            page = _page(ctx)
            logged = _logged_in(page) or (_auto_login(page) and _logged_in(page))
            log.info("ApplyWorker: браузер поднят (logged_in={}), жду клики…", logged)
            while True:
                try:
                    vid, url, cover, name, done, box = self._q.get(timeout=self.IDLE_TIMEOUT)
                except queue.Empty:
                    log.info("ApplyWorker: простой {}с — закрываю браузер, отпускаю lock",
                             self.IDLE_TIMEOUT)
                    break
                try:
                    box["result"] = ({"status": "no-session", "letter": False} if not logged
                                     else _apply_one_vacancy(page, vid, url, cover, name))
                except Exception as e:
                    box["result"] = {"status": "error", "letter": False, "error": str(e)}
                finally:
                    done.set()
                if logged:                        # владеем браузером -> дожать очередь ленты
                    with contextlib.suppress(Exception):
                        _drain_pending(page, DAILY_CAP_DEFAULT)
        except Exception as e:                    # браузер не поднялся / упал в цикле — не роняем поток
            log.error("ApplyWorker: браузер/цикл упал: {}", e)
        finally:
            with contextlib.suppress(Exception):
                if ctx is not None:
                    ctx.close()
            with contextlib.suppress(Exception):
                if p is not None:
                    p.stop()
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)     # отпустить single-instance lock
            with self._start_lock:
                self._thread = None
            # Крэш до/во время цикла (напр. браузер не поднялся) оставил бы задания в очереди
            # с невыставленным done -> HTTP-поток завис бы навсегда. Сливаем остаток ошибкой.
            self._drain("error")

    def _drain(self, status: str) -> None:
        """Слить очередь заданным статусом (напр. busy, когда lock занят кроном)."""
        while True:
            try:
                *_, done, box = self._q.get_nowait()
            except queue.Empty:
                break
            box["result"] = {"status": status, "letter": False}
            done.set()


_apply_worker: "ApplyWorker | None" = None
_apply_worker_lock = threading.Lock()


def get_apply_worker(headless: bool = True) -> ApplyWorker:
    """Синглтон тёплого воркера для сервера (создаётся лениво, потокобезопасно).
    Сервер — ThreadingHTTPServer: без лока два параллельных POST /api/apply создали бы
    два воркера -> два Chromium на один persistent-профиль -> краш."""
    global _apply_worker
    if _apply_worker is None:                    # double-checked: быстрый путь без лока
        with _apply_worker_lock:
            if _apply_worker is None:
                _apply_worker = ApplyWorker(headless=headless)
    return _apply_worker
