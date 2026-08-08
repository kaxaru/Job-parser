"""Отложенные вакансии-опросники (нужна ручная форма с вопросами работодателя).

Автоклик такие НЕ заполняет (вопросы специфичны, бот наврёт) — складывает сюда,
чтобы ты потом прошёл их руками/полу-авто. Лента может подсветить их бейджем «форма».
Файл data/form_vacancies.json: { "<vacancyId>": {"name","url","ts"} }.
"""
import datetime
import json
from threading import Lock
from typing import Any

from hrwork.config import DATA_DIR, log
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

CHAT_MESSAGES_FILE = DATA_DIR / "chat_messages.json"       # {vacancyId: {chatId, state, messages}}
FORM_VACANCIES_FILE = DATA_DIR / "form_vacancies.json"
FORM_CACHE_FILE = DATA_DIR / "forms_cache.json"            # {vacancyId: снятая структура анкеты}
RESPONSE_STATUS_FILE = DATA_DIR / "response_status.json"   # {vacancyId: employerState}
APPLIED_LOG_FILE = DATA_DIR / "applied_log.jsonl"          # журнал откликов (append-only)
PENDING_FILE = DATA_DIR / "apply_pending.json"             # очередь ожидания (лента -> крон)


def load_statuses() -> dict[str, Any]:
    out: dict[str, Any] = read_json_or(RESPONSE_STATUS_FILE, {})
    return out


def save_statuses(statuses: dict[str, Any]) -> None:
    """Карту {vacancyId: employerState} на диск (атомарно). Полная перезапись —
    статусы меняются, старые не нужны."""
    atomic_write_json(RESPONSE_STATUS_FILE, statuses, indent=0)


def load_form_vacancies() -> dict[str, Any]:
    out: dict[str, Any] = read_json_or(FORM_VACANCIES_FILE, {})
    return out


def load_chat_messages() -> dict[str, Any]:
    """Переписка по вакансиям (наполняет sync_statuses — он и так тянет chat_data)."""
    out: dict[str, Any] = read_json_or(CHAT_MESSAGES_FILE, {})
    return out


def save_chat_messages(data: dict[str, Any]) -> None:
    atomic_write_json(CHAT_MESSAGES_FILE, data, indent=0)


_APPEND_LOCK = Lock()   # сериализует дозапись внутри процесса (сервер ленты — многопоточный)


def append_applied(vid: str, name: str, url: str, via: str,
                   status: str = "applied", ts: str = "", employer: str = "") -> None:
    """Дозаписать факт отклика в журнал (append-only JSONL, по строке на отклик).

    ПИСАТЕЛЕЙ ТРИ, И ОНИ НЕ СЕРИАЛИЗОВАНЫ ОБЩИМ LOCK'ОМ (докстринг до 08.08.2026 обещал
    обратное — «single-instance lock гарантирует, что активен лишь один писатель»; это было
    неправдой): крон-батч (via='cron') и лента (via='feed') действительно ходят под
    `autoclick.lock`, а `autoclick.py::sync_statuses` пишет сюда БЕЗ него и осознанно —
    иначе синк снова встанет в очередь за откликами и статусы замрут на недели
    (docs/apply.md, инцидент «Статусы стояли неделями»).

    Целостность строки держится НЕ на lock'е, а на форме записи: одна короткая строка
    (~200 байт) уходит одним `write` в файл, открытый на дозапись, — ОС такие записи
    сериализует, поэтому смешать две строки нельзя. Внутрипроцессный `_APPEND_LOCK`
    закрывает второй случай: несколько потоков сервера ленты в ОДНОМ процессе.
    Восстановление: запись целиком либо есть, либо её нет; повтор безопасен — дубль по id
    схлопывает `funnel.py` в ранний ts, а `load_applied_log` пропускает битую строку.
    Наблюдаемость: расхождение «отклик есть на HH, строки нет» ловит `sync_statuses`
    (дожурналирует с via='hh') и строка «Синк: … новых в журнал N».

    ts пустой -> сейчас (локальное). employer фиксируется В МОМЕНТ отклика: вакансия уйдёт
    из выдачи — воронка по компаниям (funnel.py) не потеряет работодателя (fix.md №9)."""
    rec = {
        "id": str(vid), "name": name or "", "url": url or "", "via": via, "status": status,
        "ts": ts or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "employer": employer or "",
    }
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK, open(APPLIED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)                     # ОДИН write на запись — единица атомарности


def load_applied_log() -> list[dict[str, Any]]:
    """Журнал откликов -> список записей в порядке записи (старые первыми). Битые строки
    пропускаем (журнал append-only — частичная строка не должна ронять чтение)."""
    if not APPLIED_LOG_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
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
def load_pending() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = read_json_or(PENDING_FILE, [])
    return out


def _save_pending(items: list[dict[str, Any]]) -> None:
    atomic_write_json(PENDING_FILE, items, indent=0)


def enqueue_pending(vid: str, url: str, name: str, cover: str, employer: str = "") -> int:
    """Добавить вакансию в конец очереди (идемпотентно по id). Возвращает позицию в очереди.

    `employer` кладётся В МОМЕНТ КЛИКА и опционален: лента его знает (карточка перед глазами),
    а к моменту дренажа вакансия может уже уйти из выдачи. Дальше он уезжает в журнал —
    без него карточка-призрак не находится по компании (инцидент 01.08.2026)."""
    items = load_pending()
    if any(str(x.get("id")) == str(vid) for x in items):
        return len(items)                       # уже в очереди — не дублируем
    items.append({"id": str(vid), "url": url, "name": name, "cover": cover,
                  "employer": employer})
    _save_pending(items)
    return len(items)


def pop_pending_one() -> dict[str, Any] | None:
    """Снять ПЕРВЫЙ элемент очереди (FIFO, атомарно). None — пусто. По одному, чтобы
    подхватывать добавленные во время дренажа.

    Снятие БЕЗВОЗВРАТНО: запись пропала с диска раньше, чем её обработали. Тот, кто её снял,
    обязан либо довести обработку до конца, либо вернуть запись через `requeue_pending`
    (см. `autoclick.py::_drain_pending`)."""
    items = load_pending()
    if not items:
        return None
    first = items.pop(0)
    _save_pending(items)
    return first


def requeue_pending(rec: dict[str, Any]) -> int:
    """Вернуть в КОНЕЦ очереди запись, снятую `pop_pending_one`, но не обработанную.
    Возвращает длину очереди; идемпотентно по id.

    Отличие от `enqueue_pending` — кладём запись ЦЕЛИКОМ, а не пересобираем её из четырёх
    полей: возврат не имеет права терять то, чего вызывающий не знает. В конец, а не в
    начало, чтобы сбойная запись не блокировала остальную очередь; защиту от бесконечного
    круга держит вызывающий (одна попытка на запись за прогон)."""
    items = load_pending()
    if any(str(x.get("id")) == str(rec.get("id")) for x in items):
        return len(items)                       # уже вернулась/добавлена — не дублируем
    items.append(dict(rec))
    _save_pending(items)
    return len(items)


def add_form_vacancy(vid: str, name: str, url: str, ts: str = "") -> bool:
    """Добавить вакансию-опросник (идемпотентно по id). Атомарная запись.
    True — записали новую, False — уже лежала в очереди.

    Возврат нужен вызывающему для СЧЁТЧИКА: анкета — не отклик и не пропуск, и пока она
    молча падала в очередь, массовое включение опросников на HH было неотличимо от нормы
    (`autoclick.py::_apply_batch`, строка «Итог прогона»)."""
    data = load_form_vacancies()
    if vid in data:
        log.debug("Вакансия-опросник уже в форм-очереди: {} ({})", name, vid)
        return False
    data[vid] = {"name": name, "url": url, "ts": ts}
    atomic_write_json(FORM_VACANCIES_FILE, data, indent=0)
    log.info("Вакансия-опросник отложена в форму-очередь: {} ({})", name, vid)
    return True


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
def load_form_cache() -> dict[str, Any]:
    out: dict[str, Any] = read_json_or(FORM_CACHE_FILE, {})
    return out


def cache_form(vid: str, name: str, url: str, fields: list[Any],
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
