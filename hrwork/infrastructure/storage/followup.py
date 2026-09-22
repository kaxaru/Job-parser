"""Отложенные вакансии-опросники (нужна ручная форма с вопросами работодателя).

Автоклик такие НЕ заполняет (вопросы специфичны, бот наврёт) — складывает сюда,
чтобы ты потом прошёл их руками/полу-авто. Лента может подсветить их бейджем «форма».
Файл data/form_vacancies.json: { "<vacancyId>": {"name","url","ts"} }.
"""
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from hrwork.config import ACCOUNT, ACCOUNT_DIR, DATA_DIR, log
from hrwork.infrastructure.storage import atomic_write_json, read_json_or

from .filelock import file_lock

# Состояние АККАУНТА (RFC-004): у main ACCOUNT_DIR == DATA_DIR, пути прежние. Общий только
# кеш снятых анкет — структура формы не зависит от того, кто откликается.
CHAT_MESSAGES_FILE = ACCOUNT_DIR / "chat_messages.json"    # {vacancyId: {chatId, state, messages}}
# Очередь анкет — своя: общая дренировалась бы анкетным прогоном main, и вакансия из доли
# второго аккаунта ушла бы откликом от основного (A/B и «одна вакансия — один аккаунт»).
FORM_VACANCIES_FILE = ACCOUNT_DIR / "form_vacancies.json"
FORM_CACHE_FILE = DATA_DIR / "forms_cache.json"            # {vacancyId: снятая структура анкеты}
RESPONSE_STATUS_FILE = ACCOUNT_DIR / "response_status.json"  # {vacancyId: employerState}
APPLIED_LOG_FILE = ACCOUNT_DIR / "applied_log.jsonl"       # журнал откликов (append-only)
PENDING_FILE = ACCOUNT_DIR / "apply_pending.json"          # очередь ожидания (лента -> крон)


def load_statuses(path: Path | None = None) -> dict[str, Any]:
    """Статусы; `path` — файл ДРУГОГО аккаунта (RFC-004), по умолчанию свой."""
    out: dict[str, Any] = read_json_or(path or RESPONSE_STATUS_FILE, {})
    return out


def save_statuses(statuses: dict[str, Any]) -> None:
    """Карту {vacancyId: employerState} на диск (атомарно). Полная перезапись —
    статусы меняются, старые не нужны."""
    atomic_write_json(RESPONSE_STATUS_FILE, statuses, indent=0)


def load_form_vacancies() -> dict[str, Any]:
    out: dict[str, Any] = read_json_or(FORM_VACANCIES_FILE, {})
    return out


def load_chat_messages(path: Path | None = None) -> dict[str, Any]:
    """Переписка по вакансиям (наполняет sync_statuses — он и так тянет chat_data).
    `path` — файл ДРУГОГО аккаунта (RFC-004), по умолчанию свой."""
    out: dict[str, Any] = read_json_or(path or CHAT_MESSAGES_FILE, {})
    return out


def save_chat_messages(data: dict[str, Any]) -> None:
    atomic_write_json(CHAT_MESSAGES_FILE, data, indent=0)


_APPEND_LOCK = Lock()   # сериализует дозапись внутри процесса (сервер ленты — многопоточный)


@dataclass(frozen=True)
class JournalRow:
    """Строка журнала откликов (`applied_log.jsonl`) — VO вместо восьми параметров записи.

    `via` — wire-код канала (`ApplyChannel.code`): сам канал — доменный тип прикладного слоя
    (`apply/outcome.py`), а storage на application не зависит (стрелки зависимостей направлены
    внутрь, docs/architecture.md). На границе файла хранится код, а VO -> код переводит
    владелец enum'а (`runtime/store.py::log_applied`). Осознанный компромисс, не дефект.

    Порядок полей и значения по умолчанию — прежние дефолты `append_applied`."""
    vid: str
    name: str
    url: str
    via: str
    status: str = "applied"
    ts: str = ""
    employer: str = ""
    ab: bool | None = None


def append_applied(row: JournalRow) -> None:
    """Дозаписать факт отклика в журнал (append-only JSONL, по строке на отклик).

    ПИСАТЕЛЕЙ ТРИ, И ОНИ НЕ СЕРИАЛИЗОВАНЫ ОБЩИМ LOCK'ОМ (докстринг до 08.08.2026 обещал
    обратное — «single-instance lock гарантирует, что активен лишь один писатель»; это было
    неправдой): крон-батч (via='cron') и лента (via='feed') действительно ходят под
    `autoclick.lock`, а `hh_sync.py::sync_statuses` пишет сюда БЕЗ него и осознанно —
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
    из выдачи — воронка по компаниям (funnel.py) не потеряет работодателя (fix.md №9).

    `account` — код аккаунта, от которого ушёл отклик (RFC-004): на localhost видно, чьё резюме
    получил работодатель. Строки до RFC-004 поля не имеют и принадлежат владельцу файла.
    `ab` — была ли вакансия в общей части A/B-сплита; None (синк, одиночный аккаунт) — поле
    не пишется: «неизвестно» не должно читаться анализом как «вне сплита»."""
    rec: dict[str, Any] = {
        "id": str(row.vid), "name": row.name or "", "url": row.url or "",
        "via": row.via, "status": row.status,
        "ts": row.ts or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "employer": row.employer or "", "account": ACCOUNT.code,
    }
    if row.ab is not None:
        rec["ab"] = row.ab
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    # Каталог САМОГО журнала, а не DATA_DIR: у второго аккаунта журнал лежит в
    # data/accounts/<code>/, и первым писателем там бывает синк — mkdir DATA_DIR не спас бы
    # от FileNotFoundError (RFC-004). Для основного аккаунта это тот же DATA_DIR.
    APPLIED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK, open(APPLIED_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)                     # ОДИН write на запись — единица атомарности


def load_applied_log(path: Path | None = None) -> list[dict[str, Any]]:
    """Журнал откликов -> список записей в порядке записи (старые первыми). Битые строки
    пропускаем (журнал append-only — частичная строка не должна ронять чтение).
    `path` — журнал ДРУГОГО аккаунта (RFC-004); по умолчанию свой."""
    log_file = path or APPLIED_LOG_FILE
    if not log_file.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
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
#    -> файловая блокировка (см. `_pending_guard`). ──
_PENDING_LOCK = Lock()   # внутрипроцессный: сервер ленты — многопоточный (ThreadingHTTPServer)
# Межпроцессная блокировка (аудит 22.09.2026, §1). Писателей ДВА ПРОЦЕССА: сервер ленты
# (`server.py::_apply_post` -> `store.enqueue`) и владелец браузера
# (`autoclick.py::_drain_pending` -> `pop_pending_one`/`requeue_pending`). Цепочка
# «прочитал -> изменил -> записал» уходит под замок ЦЕЛИКОМ (образец — `marks.py::update_marks`):
# под одной лишь записью клик, сделанный между чтением и записью, всё равно теряется. Гонка за
# общий фиксированный tmp-файл `atomic_write_json` даёт битый JSON, а `read_json_or` отдаёт
# пустой список — очередь исчезала бы ЦЕЛИКОМ (на Windows — ещё и падение `os.replace` на
# занятом вторым процессом `.tmp`, воспроизведено тестом конкурентных `enqueue`).
# Запись длится миллисекунды, поэтому ожидание дольше таймаута — авария: писатель падает, а не
# пишет без блокировки (молча потерянный клик дороже упавшего запроса ленты).
PENDING_LOCK_TIMEOUT_S = 30.0


def _pending_lock_path() -> Path:
    # от PENDING_FILE в момент вызова: тест, подменивший очередь, блокирует свой каталог
    return PENDING_FILE.with_name(PENDING_FILE.name + ".lock")


@contextlib.contextmanager
def _pending_guard() -> Iterator[None]:
    """Замок очереди на время всего «прочитал -> изменил -> записал» (процессы + потоки)."""
    with _PENDING_LOCK, file_lock(_pending_lock_path(), timeout=PENDING_LOCK_TIMEOUT_S):
        yield


def load_pending(path: Path | None = None) -> list[dict[str, Any]]:
    """Очередь ожидания; `path` — очередь ДРУГОГО аккаунта (RFC-004), по умолчанию своя."""
    out: list[dict[str, Any]] = read_json_or(path or PENDING_FILE, [])
    return out


def _save_pending(items: list[dict[str, Any]]) -> None:
    """Запись очереди. Вызывать ТОЛЬКО под `_pending_guard` — иначе теряется чужой клик."""
    atomic_write_json(PENDING_FILE, items, indent=0)


def enqueue_pending(vid: str, url: str, name: str, cover: str, employer: str = "") -> int:
    """Добавить вакансию в конец очереди (идемпотентно по id). Возвращает позицию в очереди.

    `employer` кладётся В МОМЕНТ КЛИКА и опционален: лента его знает (карточка перед глазами),
    а к моменту дренажа вакансия может уже уйти из выдачи. Дальше он уезжает в журнал —
    без него карточка-призрак не находится по компании (инцидент 01.08.2026)."""
    with _pending_guard():
        items = load_pending()
        if any(str(x.get("id")) == str(vid) for x in items):
            return len(items)                   # уже в очереди — не дублируем
        items.append({"id": str(vid), "url": url, "name": name, "cover": cover,
                      "employer": employer})
        _save_pending(items)
        return len(items)


def pop_pending_one() -> dict[str, Any] | None:
    """Снять ПЕРВЫЙ элемент очереди (FIFO, под замком очереди). None — пусто. По одному, чтобы
    подхватывать добавленные во время дренажа.

    Снятие БЕЗВОЗВРАТНО: запись пропала с диска раньше, чем её обработали. Тот, кто её снял,
    обязан либо довести обработку до конца, либо вернуть запись через `requeue_pending`
    (см. `autoclick.py::_drain_pending`)."""
    with _pending_guard():
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
    with _pending_guard():
        items = load_pending()
        if any(str(x.get("id")) == str(rec.get("id")) for x in items):
            return len(items)                   # уже вернулась/добавлена — не дублируем
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
