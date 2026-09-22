"""Браузерные примитивы hh.ru (Playwright): запуск Chromium, навигация, вход, поднятие резюме.

Вынесено из `autoclick.py` (аудит 22.09.2026, §5). Интерфейс был де-факто публичным: `forms.py`
дважды импортировал из `autoclick` эти примитивы, то есть существовал цикл `forms <-> autoclick`,
разорванный локальными импортами внутри функций. Теперь и `forms`, и `autoclick` берут
примитивы отсюда, и цикл снят.

Сессия — persistent-профиль Chromium в `PROFILE_DIR` (гитигнорен вместе с data/):
  1) python hh.py autoclick --login   — ОДИН раз, с окном: вход по HH_EMAIL+HH_PASSWORD
     (.env) автоматом; капчу/код (если HH попросит) вводишь руками.
  2) python hh.py autoclick           — headless: поднять резюме + отклики (по крону);
     протухшая сессия самовосстанавливается автовходом по паролю.

Селекторы — data-qa-атрибуты HH и текстовые локаторы Playwright: хэшированные
magritte-классы (напр. magritte-input___LVTID_9-4-29) меняются каждым деплоем HH
и как селекторы непригодны.
"""
import contextlib
import os
import re
from enum import Enum
from typing import Any

from hrwork.application.apply import account_session, session, sms
from hrwork.application.apply.runtime import lock
from hrwork.config import ACCOUNT, ACCOUNT_DIR, log
from hrwork.domain.account import LOGIN_PHONE
from hrwork.infrastructure.sources.hh import BROWSER_UA

LOGIN_URL   = "https://hh.ru/account/login"
RESUMES_URL = "https://hh.ru/applicant/resumes"
HOME_URL    = "https://hh.ru/"   # лёгкая страница для проверки логина (не тяжёлый /resumes)
PROFILE_DIR = ACCOUNT_DIR / "browser_profile"   # свой Chromium-профиль у аккаунта
HH_EMAIL    = os.getenv("HH_EMAIL", "").strip()
HH_PASSWORD = os.getenv("HH_PASSWORD", "").strip()
# Телефон для входа по SMS (аккаунт с login="phone"). Даётся ТОЛЬКО в рантайме и НИГДЕ не
# хранится: ни в account.json, ни в логах (в лог идут лишь последние 4 цифры). Пусто -> вход
# по телефону деградирует в ручной (--login с окном).
HR_LOGIN_PHONE = os.getenv("HR_LOGIN_PHONE", "").strip()

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

# Вход по телефону+SMS (login="phone", не-основной аккаунт). Селекторы сняты с живой формы
# 15.09.2026: экран 1 — submit-button «Войти»; экран 2 — телефон выбран по умолчанию (radio
# credential-type-phone), код +7 стоит, номер идёт в phone-input-national-number-input,
# submit-button «Дальше» шлёт SMS; экран 3 — поле OTP (одно поле или ячейки по цифре).
_CRED_PHONE   = '[data-qa="credential-type-phone"]'
_PHONE_INPUT  = '[data-qa="magritte-phone-input-national-number-input"], input[inputmode="tel"]'
_OTP_INPUT    = ('[data-qa="magritte-otp-input"], input[autocomplete="one-time-code"], '
                 '[data-qa="applicant-login-input-code"], input[name="code"]')


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
# Рабочий приём тот же, что уже доказан watchdog'ом (`runtime/watchdog.py`): залипший вызов
# не отпускает python ничем, кроме СМЕРТИ ПРОЦЕССА, поэтому дедлайн живёт на уровне прогона
# (WATCHDOG_KILL_S, 50 минут — меньше ExecutionTimeLimit задачи в 60), а не отдельного вызова.


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


class LoginState(Enum):
    """Исход проверки входа по лёгкой главной — ТРИ состояния, а не булево.

    `account_session.SessionState` — другое понятие: это ЗАПИСАННЫЙ статус сессии аккаунта
    (ok/expired/foreign), сигнал для баннера ленты. Здесь — результат одного зонда страницы.

    Разведено по инциденту (аудит 22.09.2026, §1): булево схлопывало «нет сети» и «аноним»,
    и прогон на недоexавшей странице писал `record_session(EXPIRED)` и лез логиниться
    паролем при ЖИВОЙ сессии (баннер «нужен вход» врал), а на челлендж-странице без маркеров
    анонима считал себя залогиненным и выгружал куки челленджа."""
    LOGGED_IN = "logged_in"
    ANONYMOUS = "anonymous"    # аноним достоверно: форма логина в URL или маркеры в шапке
    UNKNOWN = "unknown"        # страница не доехала/не читается — НЕ «сессия протухла»


def _session_state(page: Any) -> LoginState:
    """Логин-статус по ЛЁГКОЙ главной (не тяжёлый /applicant/resumes — тот грузит весь
    список резюме и висит). Детект по маркерам анонима в шапке: кнопка «Войти»
    (data-qa=login) / форма логина (account-login-form).

    `_goto` возвращает False, если страница не доехала (DDoS-Guard, сеть, упавший контекст) —
    это UNKNOWN: вызывающий обязан остановиться с WARNING, НЕ писать `record_session(EXPIRED)`
    и НЕ входить паролем. Нечитаемая шапка/URL — тоже UNKNOWN, а не «аноним»."""
    if not _goto(page, HOME_URL):               # лёгкая страница + переждать DDoS-Guard
        return LoginState.UNKNOWN
    with contextlib.suppress(Exception):        # дождаться отрисовки шапки (SPA)
        page.wait_for_selector('[data-qa^="mainmenu"]', timeout=10_000)
    page.wait_for_timeout(1_500)                # дать осесть возможной навигации
    with contextlib.suppress(Exception):
        if "/account/login" in page.url or "/account/signup" in page.url:
            return LoginState.ANONYMOUS
    ok: bool | None = None
    try:
        ok = page.locator(_ANON_MARKERS).count() == 0
    except Exception:                           # навигация в момент count -> перепроверим
        page.wait_for_timeout(1_500)
        try:
            ok = page.locator(_ANON_MARKERS).count() == 0
        except Exception:
            return LoginState.UNKNOWN
    if not ok:
        return LoginState.ANONYMOUS
    # Живая сессия -> выгружаем куки для HTTP-синка (он работает без браузера и без lock).
    session.save_state(page.context)
    return LoginState.LOGGED_IN


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


def _national_number(raw: str) -> str:
    """10 национальных цифр РФ из любого ввода (`+7…`, `8…`, `7…`, `10 цифр`) или "" если
    столько не набралось. Чистая — тестируется без браузера."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    return d if len(d) == 10 else ""


def _fill_otp(page: Any, code: str) -> bool:
    """Ввести код: одно поле OTP -> целиком; несколько ячеек `maxlength=1` -> по цифре."""
    if _try_fill(page, _OTP_INPUT, code):
        return True
    with contextlib.suppress(Exception):        # запасной путь — ячейки по одной цифре
        cells = page.locator('input[inputmode="numeric"], input[maxlength="1"]')
        vis = [cells.nth(i) for i in range(cells.count()) if cells.nth(i).is_visible()]
        if len(vis) == len(code):
            for cell, ch in zip(vis, code):
                cell.fill(ch)
            return True
    return False


def _phone_login(page: Any) -> bool:
    """Автовход по телефону+SMS (аккаунт с login="phone", RFC-004). Флоу с живой формы:
    «Войти» -> телефон (radio по умолчанию) -> номер -> «Дальше» (шлёт SMS) -> код из
    уведомлений Windows («Связь с телефоном») -> подтверждение.

    Номер — из env `HR_LOGIN_PHONE`, нигде не сохраняется. Капча привязана к аккаунту и
    снимается только человеком -> при ней стоп (False), доводишь вручную (--login).
    True — вошли; False — нет телефона / SMS не пойман / капча / изменилась вёрстка."""
    phone = _national_number(HR_LOGIN_PHONE)
    if not phone:
        log.warning("Аккаунт {}: login=phone, но HR_LOGIN_PHONE не задан (нужно 10 нац. цифр) — "
                    "войди вручную: HR_ACCOUNT={} hh.py autoclick --login", ACCOUNT.code, ACCOUNT.code)
        return False
    if not _goto(page, LOGIN_URL):
        log.warning("DDoS-Guard не пройден на странице логина — headed/повтори")
    page.wait_for_timeout(2_000)
    if is_captcha(page):
        log.warning("HH показал капчу на входе — доведи вручную (--login)")
        return False
    log.debug("phone-login[{}]: экран «Войти», номер …{}", ACCOUNT.code, phone[-4:])
    _try_click(page, _SUBMIT)                     # экран 1 -> «Войти»
    page.wait_for_timeout(2_500)
    if is_captcha(page):
        log.warning("HH показал капчу после «Войти» — доведи вручную (--login)")
        return False
    _try_click(page, _CRED_PHONE, force=True)     # телефон по умолчанию, выбираем явно на всякий
    baseline = sms.latest_notification_id()       # отметка «до запроса SMS» — до нажатия «Дальше»
    if not _try_fill(page, _PHONE_INPUT, phone):
        log.warning("Аккаунт {}: поле номера не найдено — вёрстка формы изменилась", ACCOUNT.code)
        return False
    page.wait_for_timeout(500)
    with contextlib.suppress(Exception):          # маска телефона могла не принять .fill
        loc = page.locator(_PHONE_INPUT).first
        if re.sub(r"\D", "", loc.input_value() or "")[-10:] != phone:
            loc.click()
            loc.press("Control+A")
            loc.press("Delete")
            loc.type(phone, delay=80)
    _try_click(page, _SUBMIT)                      # «Дальше» -> запрос SMS
    page.wait_for_timeout(3_000)
    if is_captcha(page):
        log.warning("HH показал капчу после запроса кода — доведи вручную (--login)")
        return False
    log.info("Аккаунт {}: жду код из SMS до 90с…", ACCOUNT.code)
    code = sms.read_login_code(baseline)
    if not code:
        log.warning("Аккаунт {}: код из SMS не пойман (нет «Связи с телефоном»/капча) — "
                    "войди вручную: HR_ACCOUNT={} hh.py autoclick --login", ACCOUNT.code, ACCOUNT.code)
        return False
    log.debug("phone-login[{}]: код получен, ввожу", ACCOUNT.code)
    if not _fill_otp(page, code):
        log.warning("Аккаунт {}: поле кода не найдено — вёрстка формы изменилась", ACCOUNT.code)
        return False
    page.wait_for_timeout(1_000)
    _try_click(page, _SUBMIT)                      # подтвердить код (если кнопка есть)
    for _ in range(20):                            # ждём исчезновения формы (до ~40с)
        page.wait_for_timeout(2_000)
        if is_captcha(page):
            log.warning("HH показал капчу на подтверждении — доведи вручную (--login)")
            return False
        if not _on_login_form(page):
            break
    return not _on_login_form(page)


def _auto_login(page: Any) -> bool:
    """Единая точка входа для всех аккаунтов; способ выбирается по `ACCOUNT.login`:
    `password` (основной) — автомат по HH_EMAIL(+HH_PASSWORD); `phone` — авто по телефону+SMS
    (`_phone_login`); `manual` — не входит сам, окно ждёт ручного ввода (возвращает False).

    Через эту функцию проходят run, ApplyWorker и login — поэтому проверка
    способа входа стоит ЗДЕСЬ. HH_EMAIL/HH_PASSWORD — учётка ОСНОВНОГО аккаунта; ни один
    дополнительный аккаунт их не вводит (форму входа второго они бы скомпрометировали).

    Ниже — автомат для основного (АДАПТИВНЫЙ: на каждой итерации смотрит, какой элемент формы
    сейчас на экране, и делает один шаг; устойчив к перерисовкам DDoS-Guard и порядку полей).
    Форма /account/login многошаговая: тип аккаунта -> e-mail -> «войти по паролю» -> пароль.

    True — вошли; False — застряли (капча / нет пароля / изменилась вёрстка)."""
    if not ACCOUNT.is_main:
        if ACCOUNT.login == LOGIN_PHONE:
            return _phone_login(page)
        # login="manual": вход только вручную в окне (--login), автомат ничего не вводит.
        log.warning("Аккаунт {}: автовход не настроен (login={}) — вход вручную в окне "
                    "(HR_ACCOUNT={} hh.py autoclick --login)", ACCOUNT.code, ACCOUNT.login, ACCOUNT.code)
        return False
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
    with lock._single_instance(), sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = _page(ctx)
        try:
            if _session_state(page) is LoginState.LOGGED_IN:
                if account_session.verify_session():
                    log.success("Уже залогинен — профиль: {}", PROFILE_DIR)
                return
            if _auto_login(page) and _session_state(page) is LoginState.LOGGED_IN:
                if account_session.verify_session():
                    log.success("Автовход выполнен — сессия сохранена: {}", PROFILE_DIR)
                return
            if not ACCOUNT.is_main:
                _goto(page, LOGIN_URL)         # ждать ручной вход на форме, а не на главной
                log.info("Аккаунт {}: войди в окне по номеру телефона и коду из SMS. "
                         "НЕ закрывай окно.", ACCOUNT.code)
            elif _captcha_shown(page):
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
            if _session_state(page) is LoginState.LOGGED_IN:
                if account_session.verify_session():
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
