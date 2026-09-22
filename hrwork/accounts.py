"""Разрешение аккаунта HH по `HR_ACCOUNT` — слой конфигурации (RFC-004).

Отдельно от `config.py`, потому что config зовёт это на импорте: тест путей второго аккаунта
подменяет `resolve_account` в дочернем процессе ДО импорта config, не заводя
`data/accounts/<code>/` в реальных данных. Отдельно от домена, потому что здесь чтение диска
(`architecture.md`: домен диска не трогает).

Модуль не импортирует `hrwork.config`.
"""
import json
from pathlib import Path

from hrwork.domain.account import (
    _ACCOUNT_LOGINS,
    ACCOUNT_META_FILE,
    ACCOUNT_PROFILE_FILE,
    LOGIN_MANUAL,
    MAIN_CODE,
    AccountError,
    HhAccount,
    accounts_dir,
    parse_account_code,
)


def resolve_account(raw: str, data_dir: Path) -> HhAccount:
    """Строгий контракт: `HR_ACCOUNT` -> аккаунт или `AccountError` с готовой подсказкой.

    Папка не создаётся ни при какой ошибке: опечатка в коде не заводит молча новый пустой
    аккаунт — с чистой квотой и без журнала он откликнулся бы на всё, что основной уже разобрал.
    Свой `resume_profile.json` обязателен: без него отбор молча взял бы дефолты, то есть
    отбор основного, — ровно то, от чего второй аккаунт и заводится."""
    code = parse_account_code(raw)
    if code == MAIN_CODE:
        return HhAccount(code=MAIN_CODE, label="основной", data_dir=data_dir)
    folder = accounts_dir(data_dir) / code
    meta_file = folder / ACCOUNT_META_FILE
    if not meta_file.is_file():
        raise AccountError(f"HR_ACCOUNT={code}: нет data/accounts/{code}/{ACCOUNT_META_FILE}. "
                           f"Аккаунт заводится вручную (RFC-004), папка сама не создаётся")
    if not (folder / ACCOUNT_PROFILE_FILE).is_file():
        raise AccountError(f"HR_ACCOUNT={code}: нет data/accounts/{code}/{ACCOUNT_PROFILE_FILE} — "
                           f"без него отбор шёл бы по дефолтам основного аккаунта")
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AccountError(f"HR_ACCOUNT={code}: {ACCOUNT_META_FILE} не читается: {e}") from e
    if not isinstance(meta, dict):
        meta = {}
    label = meta.get("label")
    # Способ входа не-основного аккаунта: phone (телефон+SMS) или manual (ручной ввод в окне).
    # password запрещён — он означал бы вход учётными данными ОСНОВНОГО (HH_EMAIL/HH_PASSWORD).
    login = str(meta.get("login") or LOGIN_MANUAL)
    if login not in _ACCOUNT_LOGINS:
        raise AccountError(f"HR_ACCOUNT={code}: login={login!r} в {ACCOUNT_META_FILE} — "
                           f"допустимо {' или '.join(_ACCOUNT_LOGINS)} (password только у основного)")
    return HhAccount(code=code, label=str(label or code), data_dir=folder, login=login)

