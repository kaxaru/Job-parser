"""Аккаунт соискателя hh.ru (RFC-004): чей браузерный профиль, сессия, квота и журнал откликов.

Один процесс — один аккаунт. Аккаунт выбирается переменной `HR_ACCOUNT` ДО импорта `hrwork`:
правила отбора и пути — константы, которые печёт импорт `config.py`, поэтому сменить аккаунт
посреди процесса нельзя, и это сознательно (иначе пришлось бы перестраивать боевой путь).

`main` живёт на прежних путях (`data/…`), миграции нет. Любой другой аккаунт — папка
`data/accounts/<code>/`, заведённая ВРУЧНУЮ. Здесь только VO и разбор кода — чистые; проверку
папки на диске делает `config.py::resolve_account` (домен диска не трогает, `architecture.md`).

Модуль не импортирует `hrwork.config`: его зовёт сам config.
"""
import re
from dataclasses import dataclass
from pathlib import Path

MAIN_CODE = "main"
ACCOUNT_META_FILE = "account.json"      # {"label": "..."} — пишет человек; телефонов и паролей нет
ACCOUNT_PROFILE_FILE = "resume_profile.json"
_CODE_RX = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class AccountError(ValueError):
    """Аккаунт не разрешился: код неизвестен или папка заведена не до конца."""


# Способы входа в аккаунт hh.ru (RFC-004). Основной — только `password` (HH_EMAIL/HH_PASSWORD
# из .env). Дополнительные не знают этих учётных данных: `phone` — авто по телефону+SMS (номер
# в рантайме, не хранится), `manual` — окно ждёт ручного ввода.
LOGIN_PASSWORD = "password"
LOGIN_PHONE = "phone"
LOGIN_MANUAL = "manual"
_ACCOUNT_LOGINS = (LOGIN_PHONE, LOGIN_MANUAL)   # допустимые для НЕ-основного


@dataclass(frozen=True)
class HhAccount:
    code: str
    label: str
    data_dir: Path          # main -> data/, иначе data/accounts/<code>/
    login: str = LOGIN_PASSWORD   # способ входа; для не-основного — из account.json

    @property
    def is_main(self) -> bool:
        return self.code == MAIN_CODE


def accounts_dir(data_dir: Path) -> Path:
    """Корень папок дополнительных аккаунтов."""
    return data_dir / "accounts"


def parse_account_code(raw: str) -> str:
    """Строгий разбор значения `HR_ACCOUNT`: пусто -> `main`, иначе безопасный код папки.
    Код становится сегментом пути, поэтому `..`, слеши и пробелы отбиваются до диска."""
    code = raw.strip()
    if code in ("", MAIN_CODE):
        return MAIN_CODE
    if not _CODE_RX.fullmatch(code):
        raise AccountError(f"HR_ACCOUNT={code!r}: код аккаунта — строчная латиница, цифры, "
                           f"«_» и «-», до 32 символов")
    return code
