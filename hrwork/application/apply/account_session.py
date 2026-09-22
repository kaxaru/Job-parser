"""Сессия и личность аккаунта HH (RFC-004): чей вход лежит в профиле и что о нём известно.

**Пин личности.** Два аккаунта — два persistent-профиля Chromium, и перепутать их легко: вход
не в тот аккаунт в окне `--login`, скопированная папка профиля, `HR_ACCOUNT`, забытый в консоли.
Последствие необратимо — отклики уходят от чужого резюме. Поэтому при первом живом входе
аккаунт запоминает, КТО в сессии: хеш куки `_hi` (id пользователя HH; её же чат-API принимает
как applicantId). Дальше каждый браузерный прогон и синк сверяют сессию с пином и с пинами
остальных аккаунтов и останавливаются при расхождении. Хранится хеш, а не сам id: сравнению
этого достаточно.

**Статус сессии** (`session_status.json`) — для баннера ленты «нужен вход». Второй аккаунт
входит только вручную по SMS, и истёкшая сессия без сигнала означала бы тихие недели без откликов.
"""
import datetime
import hashlib
from enum import Enum
from pathlib import Path

from hrwork.application.apply import session
from hrwork.config import ACCOUNT, ACCOUNT_DIR, DATA_DIR, log
from hrwork.domain.account import MAIN_CODE, accounts_dir
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

IDENTITY_FILE_NAME = "account_identity.json"
IDENTITY_FILE = ACCOUNT_DIR / IDENTITY_FILE_NAME        # {"identity": "<16 hex>", "pinned_at": ISO}
SESSION_STATUS_FILE_NAME = "session_status.json"
SESSION_STATUS_FILE = ACCOUNT_DIR / SESSION_STATUS_FILE_NAME   # {"state": SessionState, "ts": ISO}
DATA_ROOT = DATA_DIR                                    # откуда искать пины остальных аккаунтов
USER_ID_COOKIE = "_hi"


class SessionState(Enum):
    OK = "ok"                   # вход подтверждён, личность совпала
    EXPIRED = "expired"         # сессии нет, а войти сами не можем (второй аккаунт — только SMS)
    FOREIGN = "foreign"         # в сессии не тот пользователь HH — прогон остановлен


def record_session(state: SessionState) -> None:
    """Записать статус сессии аккаунта. Сбой записи не роняет прогон — это сигнал, не учёт."""
    try:
        atomic_write_json(SESSION_STATUS_FILE, {
            "state": state.value,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        })
    except OSError as e:
        log.warning("Не удалось записать статус сессии: {}", e)


def identity_from_state(state_file: Path | None = None) -> str | None:
    """Хеш id пользователя HH из сохранённого состояния сессии; None — куки нет.

    Разбор кук — общий `session.hh_cookies` (аудит 22.09.2026, §3.2): раньше фильтр по домену
    был скопирован здесь и в `session._cookies_and_xsrf`, и правка одного молча расходилась со
    вторым — а эта функция решает, чьё резюме отправит отклик (RFC-004)."""
    raw = read_json_or(state_file or session.STATE_FILE, {})
    value = session.hh_cookies(raw).get(USER_ID_COOKIE)
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _pins_of_other_accounts() -> dict[str, str]:
    """{код: пин} всех аккаунтов, кроме текущего, у которых пин уже есть."""
    files = {MAIN_CODE: DATA_ROOT / IDENTITY_FILE_NAME}
    root = accounts_dir(DATA_ROOT)
    if root.is_dir():
        files.update({d.name: d / IDENTITY_FILE_NAME for d in root.iterdir() if d.is_dir()})
    files.pop(ACCOUNT.code, None)
    pins = {code: read_json_or(path, {}).get("identity") for code, path in files.items()}
    return {code: str(pin) for code, pin in pins.items() if pin}


def identity_problem() -> str | None:
    """None — сессия принадлежит этому аккаунту (при первом входе пин записывается).
    Строка — почему прогон обязан остановиться; её печатают как есть."""
    current = identity_from_state()
    if current is None:
        log.warning("Аккаунт {}: в состоянии сессии нет куки {} — личность не сверена",
                    ACCOUNT.code, USER_ID_COOKIE)
        return None
    login_hint = f"войди заново: hh.py autoclick --login при HR_ACCOUNT={ACCOUNT.code}"
    for code, pin in _pins_of_other_accounts().items():
        if pin == current:
            return (f"Аккаунт {ACCOUNT.code}: в сессии пользователь HH аккаунта {code} — "
                    f"прогон остановлен, {login_hint}")
    pinned = read_json_or(IDENTITY_FILE, {}).get("identity")
    if not pinned:
        atomic_write_json(IDENTITY_FILE, {
            "identity": current,
            "pinned_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        log.info("Аккаунт {}: личность HH закреплена ({})", ACCOUNT.code, IDENTITY_FILE_NAME)
        return None
    if pinned != current:
        return (f"Аккаунт {ACCOUNT.code}: в сессии другой пользователь HH, не закреплённый в "
                f"{IDENTITY_FILE_NAME} — прогон остановлен, {login_hint}")
    return None


def verify_session() -> bool:
    """Сверить личность и записать статус. False — прогон обязан остановиться (причина в логе)."""
    problem = identity_problem()
    if problem is not None:
        log.error(problem)
        record_session(SessionState.FOREIGN)
        return False
    record_session(SessionState.OK)
    return True
