"""Синк статусов из чатов hh.ru — БЕЗ БРАУЗЕРА (cookie-only HTTP).

Вынесено из `autoclick.py` (аудит 22.09.2026, §5): ни одного обращения к `page`, входящих
рёбер два — CLI (`hh.py autoclick --sync-status/--sync-full`) и `_reconciled_today()` из
крон-батча (`autoclick._apply_batch`).

Почему cookie-only: чат-эндпоинты (chatik.hh.ru) НЕ гейтит Group-IB fingerprint (в отличие от
отклика и `/resumes/touch`). Раньше синк ради этих JSON-вызовов поднимал целый Chromium,
проходил DDoS-Guard и занимал общий `autoclick.lock` — из-за чего конкурировал с откликами и
намертво вставал вместе с ними (статусы в проекте застряли на две недели). Теперь браузерный
прогон сохраняет состояние сессии (`session.save_state`), а синк работает поверх него.
"""
import datetime
import random
import time
from typing import Any

from hrwork.application.apply import account_session, session, taken
from hrwork.application.apply.chat import chat
from hrwork.application.apply.outcome import ApplyChannel, VacancyMark
from hrwork.application.apply.runtime.store import store
from hrwork.config import log
from hrwork.infrastructure.storage import vacancy_repository
from hrwork.infrastructure.storage.followup import JournalRow

SYNC_FRESH_DAYS = 7          # сообщения старше -> чат считается устоявшимся
# Чекпойнт синка: как часто сбрасывать накопленное на диск. Полный прогон — ~1300 чатов и
# десятки минут, а watchdog на этот путь не распространяется (браузера нет). До 08.08.2026
# save_statuses/save_chat_messages стояли ПОСЛЕ цикла: kill на 800-м чате терял всю скачанную
# переписку целиком. 100 -> максимум 13 записей файла за прогон (2.4 МБ, атомарно).
SYNC_CHECKPOINT_EVERY = 100


def _journal_name(data: dict[str, Any], vid: str, name_map: dict[str, Any]) -> str:
    """Имя вакансии для журнала: из локального сбора, иначе из resources чата, иначе id.

    Фолбэк на id обязателен: `applied_log.jsonl` — append-only, и запись с пустым `name`
    задним числом не чинится. Вакансия навсегда осталась бы безымянной в «моих откликах»
    и в воронке. До 07.08.2026 функция обещала этот фолбэк докстрингом, но возвращала
    пустую строку, когда вакансии не было и в resources чата."""
    nm: str | None = name_map.get(vid)
    if nm:
        return nm
    vac = ((data.get("resources") or {}).get("vacancies") or {}).get(vid) or {}
    nm_res: str = vac.get("name") or ""
    return nm_res or str(vid)


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

    ПОЛИТИКА ВОССТАНОВЛЕНИЯ: статусы и переписка сбрасываются на диск КАЖДЫЕ
    SYNC_CHECKPOINT_EVERY чатов, а не одним куском в конце. Единица «выполнено целиком» —
    чекпойнт: запись атомарна и merge-семантична, поэтому убитый на 800-м из 1300 прогон
    сохраняет всё до последнего чекпойнта, а недокачанные чаты следующий прогон возьмёт
    заново (повтор идемпотентен). Журнал пишется поштучно и переживает kill сам.
    Чат без сообщений НЕ журналируется вовсе: без реальной даты запись в append-only журнал
    зафиксировала бы сегодняшнее число навсегда. Наблюдаемость — строки «чекпойнт i/N» и
    «N чатов без сообщений … журналирование отложено».

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
    # Синк пишет журнал и статусы аккаунта — из чужой сессии он влил бы чужие отклики (RFC-004).
    # Статус сессии здесь не пишем: без браузера живость входа не проверена.
    if (problem := account_session.identity_problem()) is not None:
        log.error(problem)
        req.__exit__()
        return {}
    repo = vacancy_repository().load()
    name_map = {rec.id: rec.vacancy.name for rec in repo}
    employer_map = {rec.id: (rec.vacancy.employer or "") for rec in repo}
    seen = store.applied_ids()
    added = failed = deferred = 0
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
        # RFC-004: чат на вакансии, которая в журнале или очереди ДРУГОГО аккаунта, — двойной
        # отклик уже случился (руками на hh.ru или в обход дедупа). Не чинится, но должен быть виден.
        # Считаем УНИКАЛЬНЫЕ вакансии: на одну вакансию бывает несколько чатов, и без дедупа
        # счётчик врал в 16 раз (21.09.2026: «1200 вакансий» при 72 реальных).
        others = taken.taken_by_others()
        conflicts = sorted({str(c["vacancyId"]) for c in chats
                            if str(c["vacancyId"]) in others})
        if conflicts:
            log.warning("Синк: конфликт аккаунтов — {} вакансий с откликом и здесь, и у другого "
                        "аккаунта: {}", len(conflicts),
                        ", ".join(f"{v}->{others[v]}" for v in conflicts[:20]))
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
                ts = chat.response_time(data)
                if not ts:
                    # Чат без сообщений -> даты отклика НЕТ. Раньше сюда подставлялось now()
                    # (append_applied), и старый ручной отклик навсегда журналился сегодняшним
                    # числом: журнал append-only, задним числом дата не чинится, а воронка
                    # `funnel.py` считает по ней латентность. Откладываем: сообщения появятся —
                    # следующий синк дожурналирует с настоящей датой.
                    deferred += 1
                else:
                    store.log_applied(JournalRow(
                        vid=vid, name=_journal_name(data, vid, name_map),
                        url=f"https://hh.ru/vacancy/{vid}", via=ApplyChannel.HH.code,
                        ts=ts, employer=employer_map.get(vid, "")))
                    seen.add(vid)
                    added += 1
            if i % 50 == 0:
                log.info("  …{}/{}", i, len(chats))
            if i % SYNC_CHECKPOINT_EVERY == 0:
                # ЧЕКПОЙНТ. Обе записи атомарны (tmp + os.replace) и являются MERGE поверх кеша,
                # поэтому чекпойнт — законченное состояние, а не полуфабрикат: kill сразу после
                # него теряет максимум SYNC_CHECKPOINT_EVERY последних чатов, и следующий прогон
                # их просто перекачает (повтор идемпотентен). Журнал пишется поштучно и от
                # чекпойнта не зависит.
                store.save_statuses(statuses)
                store.save_chat_messages(msgs_out)
                log.info("  чекпойнт {}/{}: статусов {}, переписок {}",
                         i, len(chats), len(statuses), len(msgs_out))
            time.sleep(random.uniform(0.1, 0.3))   # мягко, но быстрее браузерного пути
    if failed:
        log.warning("Синк: {} чатов не получены (сетевые сбои) — остались кешевыми", failed)
    if deferred:
        log.warning("Синк: {} чатов без сообщений — дата отклика неизвестна, журналирование "
                    "отложено до появления переписки (сегодняшнюю дату не подставляем)", deferred)
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
    # Синк — единственный путь, который узнаёт об отклике, не дошедшем до дневного счётчика
    # (watchdog между кликом и bump_quota, подтверждение HH позже 10с, отклик руками на hh.ru).
    # Дожурналировали -> сразу выравниваем квоту по журналу, иначе недосчёт живёт до полуночи
    # и бот шлёт cap+N (аудит 08.08.2026, находка 39).
    _reconciled_today()
    log.success("Синк: статусов {}, новых в журнал {}, из кеша (старше {} дн) {} -> {}",
                len(statuses), added, SYNC_FRESH_DAYS, from_cache,
                {chat.STATE_LABELS.get(k, k): v for k, v in counts.items()})
    return statuses


# ── Реконсиляция дневной квоты: окно «клик -> учёт» (аудит 08.08.2026, находка 39) ──
# Между кликом «Откликнуться» и `bump_quota` есть щель: watchdog может снести дерево, а HH —
# подтвердить отклик на 11-й секунде, когда `apply_one` уже вернул SKIP. Отклик при этом УШЁЛ,
# marks и журнал потом дочиняет синк из чатов, а квоту не чинил НИКТО — за день бот слал cap+N.
# Двойного отклика не возникает (кнопка HH отдаёт ветку ALREADY), но лимит HH превышался.
# Источник правды для выравнивания — append-only журнал: он единственный переживает kill и
# пополняется синком с РЕАЛЬНОЙ датой отклика, в том числе для сделанных руками на hh.ru.

def _journal_applied_today(today: str = "") -> int:
    """Сколько РАЗНЫХ вакансий журнал знает как откликнутые сегодня (дедуп по id: синк мог
    дожурналировать ту же вакансию вторым каналом).

    Дата берётся из самой записи, вместе с её смещением (наши — локальное, из чатов — +03:00).
    Расхождение смещений может уронить в «вчера» отклик, сделанный в первый час суток, —
    ошибка только В МЕНЬШУЮ сторону, то есть в сторону прежнего поведения; завысить счётчик
    и придушить дневной темп она не может. Ручные отклики (via='hh') считаются намеренно:
    лимит HH — на аккаунт, а не на бота."""
    day = today or datetime.date.today().isoformat()
    return len({str(e.get("id")) for e in store.applied_log()
                if str(e.get("ts") or "")[:10] == day})


def _reconciled_today() -> int:
    """Дневной счётчик, выровненный по журналу (только вверх). Наблюдаемость: расхождение
    печатается WARNING'ом «Квота расходится с журналом» — по нему поломка видна в эксплуатации,
    а не по вопросу пользователя «почему сегодня 35, а не 103»."""
    used = store.applied_today()
    journaled = _journal_applied_today()
    if journaled <= used:
        return used
    total = store.reconcile_quota(journaled)
    log.warning("Квота расходится с журналом: счётчик {}, в журнале за сегодня {} — выровнял "
                "до {} (отклик ушёл, а учёт не дошёл: watchdog/подтверждение позже 10с)",
                used, journaled, total)
    return total
