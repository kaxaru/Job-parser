"""Отложенные вакансии-опросники (нужна ручная форма с вопросами работодателя).

Автоклик такие НЕ заполняет (вопросы специфичны, бот наврёт) — складывает сюда,
чтобы ты потом прошёл их руками/полу-авто. Лента может подсветить их бейджем «форма».
Файл data/form_vacancies.json: { "<vacancyId>": {"name","url","ts"} }.
"""
import datetime
import json

from hrwork.config import DATA_DIR, log
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

CHAT_MESSAGES_FILE = DATA_DIR / "chat_messages.json"       # {vacancyId: {chatId, state, messages}}
FORM_VACANCIES_FILE = DATA_DIR / "form_vacancies.json"
FORM_CACHE_FILE = DATA_DIR / "forms_cache.json"            # {vacancyId: снятая структура анкеты}
RESPONSE_STATUS_FILE = DATA_DIR / "response_status.json"   # {vacancyId: employerState}
APPLIED_LOG_FILE = DATA_DIR / "applied_log.jsonl"          # журнал откликов (append-only)
PENDING_FILE = DATA_DIR / "apply_pending.json"             # очередь ожидания (лента -> крон)


def load_statuses() -> dict:
    return read_json_or(RESPONSE_STATUS_FILE, {})


def save_statuses(statuses: dict) -> None:
    """Карту {vacancyId: employerState} на диск (атомарно). Полная перезапись —
    статусы меняются, старые не нужны."""
    atomic_write_json(RESPONSE_STATUS_FILE, statuses, indent=0)


def load_form_vacancies() -> dict:
    return read_json_or(FORM_VACANCIES_FILE, {})


def load_chat_messages() -> dict:
    """Переписка по вакансиям (наполняет sync_statuses — он и так тянет chat_data)."""
    return read_json_or(CHAT_MESSAGES_FILE, {})


def save_chat_messages(data: dict) -> None:
    atomic_write_json(CHAT_MESSAGES_FILE, data, indent=0)


def append_applied(vid: str, name: str, url: str, via: str,
                   status: str = "applied", ts: str = "", employer: str = "") -> None:
    """Дозаписать факт отклика в журнал (append-only JSONL, по строке на отклик).
    Пишут крон-батч (via='cron') и лента (via='feed'); single-instance lock гарантирует,
    что одновременно активен лишь один писатель — гонок нет. ts пустой -> сейчас (локальное).
    employer фиксируется В МОМЕНТ отклика: вакансия уйдёт из выдачи — воронка по компаниям
    (funnel.py) не потеряет работодателя (fix.md №9)."""
    rec = {
        "id": str(vid), "name": name or "", "url": url or "", "via": via, "status": status,
        "ts": ts or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "employer": employer or "",
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(APPLIED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_applied_log() -> list[dict]:
    """Журнал откликов -> список записей в порядке записи (старые первыми). Битые строки
    пропускаем (журнал append-only — частичная строка не должна ронять чтение)."""
    if not APPLIED_LOG_FILE.exists():
        return []
    out: list[dict] = []
    for line in APPLIED_LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ── Очередь ожидания: лента кладёт сюда вакансию, если браузер занят кроном; тот, кто
#    владеет браузером (крон/воркер), дренажит очередь после своей работы. Разные процессы
#    -> файловая координация (атомарная запись; окно гонки мало, эффект — пропуск/повтор). ──
def load_pending() -> list[dict]:
    return read_json_or(PENDING_FILE, [])


def _save_pending(items: list[dict]) -> None:
    atomic_write_json(PENDING_FILE, items, indent=0)


def enqueue_pending(vid: str, url: str, name: str, cover: str) -> int:
    """Добавить вакансию в конец очереди (идемпотентно по id). Возвращает позицию в очереди."""
    items = load_pending()
    if any(str(x.get("id")) == str(vid) for x in items):
        return len(items)                       # уже в очереди — не дублируем
    items.append({"id": str(vid), "url": url, "name": name, "cover": cover})
    _save_pending(items)
    return len(items)


def pop_pending_one() -> dict | None:
    """Снять ПЕРВЫЙ элемент очереди (FIFO, атомарно). None — пусто. По одному, чтобы
    подхватывать добавленные во время дренажа."""
    items = load_pending()
    if not items:
        return None
    first = items.pop(0)
    _save_pending(items)
    return first


def add_form_vacancy(vid: str, name: str, url: str, ts: str = "") -> None:
    """Добавить вакансию-опросник (идемпотентно по id). Атомарная запись."""
    data = load_form_vacancies()
    if vid in data:
        return
    data[vid] = {"name": name, "url": url, "ts": ts}
    atomic_write_json(FORM_VACANCIES_FILE, data, indent=0)
    log.info("Вакансия-опросник отложена в форму-очередь: {} ({})", name, vid)


def remove_form_vacancy(vid: str) -> None:
    """Убрать вакансию из форм-очереди — ПОСЛЕ того как человек заполнил и отправил анкету.
    Без этого она всплывёт снова и провоцирует ПОВТОРНУЮ отправку работодателю (RFC-003)."""
    data = load_form_vacancies()
    if str(vid) not in data:
        return
    del data[str(vid)]
    atomic_write_json(FORM_VACANCIES_FILE, data, indent=0)
    log.info("Форма-вакансия убрана из очереди: {}", vid)


# ── Кеш снятой структуры анкет: свип открывает форму, извлекает вопросы/опции и кладёт сюда,
#    чтобы в СЛЕДУЮЩИЙ раз не гонять её через браузер повторно. Ключ — vacancyId, значение —
#    {name,url,fields:[{prompt,ftype,options}],status,ts}. status: ok | empty | error. ──
def load_form_cache() -> dict:
    return read_json_or(FORM_CACHE_FILE, {})


def cache_form(vid: str, name: str, url: str, fields: list,
               status: str, ts: str = "") -> None:
    """Запомнить структуру анкеты (идемпотентно по id, полная перезапись — записей немного).
    Пишется по одной форме сразу после извлечения (crash-safe: обрыв свипа не теряет собранное)."""
    data = load_form_cache()
    data[str(vid)] = {
        "name": name or "", "url": url or "", "fields": fields, "status": status,
        "ts": ts or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_write_json(FORM_CACHE_FILE, data, indent=0)


def cached_form_ids() -> set[str]:
    """id всех форм, чью структуру свип уже снял — чтобы пропускать при повторном проходе."""
    return set(load_form_cache().keys())
