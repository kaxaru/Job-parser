"""Чтение кода входа hh.ru из SMS (RFC-004): вход по телефону без ручного ввода кода.

Телефон пользователя связан с ПК через «Связь с телефоном», и SMS дублируется в уведомления
Windows (`wpndatabase.db`). Отсюда берётся код входа. Только чтение; на не-Windows или без базы
всё деградирует в None — вход тогда доводится вручную в окне.

Личных данных модуль НЕ хранит: номер телефона в него не попадает вовсе (его вводит
`autoclick`), а код используется разово для ввода в форму и никуда не пишется.
"""
import html
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from hrwork.config import log

# Код в тексте hh.ru («hh.ru 1234 — это код…» / «код: 1234»). Пробел до 20 символов покрывает
# формулировки, но не даёт зацепить случайное число из другого предложения.
_CODE_RX = re.compile(r"hh\.ru\D{0,20}(\d{4,8})|(?:код|code|пароль)\D{0,20}(\d{4,8})", re.I)
_WPN_REL = r"Microsoft\Windows\Notifications\wpndatabase.db"


def notifications_db() -> Path | None:
    """Путь к базе уведомлений Windows или None (нет LOCALAPPDATA / не Windows / файла нет)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    db = Path(base) / _WPN_REL
    return db if db.exists() else None


def extract_code(text: str) -> str | None:
    """Код входа из текста уведомления hh.ru; None — не найден. Чистая (тестируется без БД)."""
    m = _CODE_RX.search(text or "")
    return (m.group(1) or m.group(2)) if m else None


def _rows_since(db: Path, after_id: int) -> list[tuple[int, str]]:
    """(Id, текст) уведомлений с Id > after_id. База в WAL, читаем временную копию и удаляем её."""
    tmp = tempfile.mkdtemp(prefix="hh_sms_")
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(tmp) / ("w.db" + suffix))
        con = sqlite3.connect(str(Path(tmp) / "w.db"))
        try:
            raw = con.execute("select Id, Payload from Notification where Id > ? order by Id",
                              (after_id,)).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error) as e:
        # WARNING, а не тихий []: сломанная/недоступная wpndatabase.db делала отказ НЕОТЛИЧИМЫМ
        # от «SMS не пришла», владелец шёл вводить код руками и не знал, что дело в базе
        # (аудит 22.09.2026, §1).
        log.warning("SMS: база уведомлений Windows недоступна ({}: {}) — код из SMS не пойман, "
                    "вход доводи вручную (--login)", type(e).__name__, e)
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = []
    for rid, payload in raw:
        body = payload.decode("utf-8", "ignore") if isinstance(payload, (bytes, bytearray)) else str(payload or "")
        text = " ".join(html.unescape(t) for t in re.findall(r"<text[^>]*>(.*?)</text>", body, re.S))
        out.append((int(rid), text))
    return out


def latest_notification_id() -> int:
    """Наибольший Id уведомления сейчас — отметка «до запроса кода» (0, если базы нет)."""
    db = notifications_db()
    return max((rid for rid, _ in _rows_since(db, -1)), default=0) if db else 0


def read_login_code(after_id: int, timeout_s: float = 90.0, poll_s: float = 2.0) -> str | None:
    """Код входа hh.ru из SMS, пришедшей ПОСЛЕ `after_id`; ждёт до `timeout_s`. None — не пойман
    (нет базы, «Связь с телефоном» не передала SMS, hh.ru показал капчу вместо кода)."""
    db = notifications_db()
    if db is None:
        return None
    deadline = time.monotonic() + timeout_s
    while True:
        for _rid, text in _rows_since(db, after_id):
            if "hh.ru" in text.lower() and (code := extract_code(text)):
                return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)
