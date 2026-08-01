"""Автоответы в чаты по шаблонам: связка триажа (chat_class) + движка (chat_answer)
+ транспорта (chat.send_message).

БЕЗОПАСНОСТЬ (контракт из docs/security.md — «отправка по флагу»):
  * по умолчанию DRY-RUN — только показать, что ушло бы; отправка лишь с --send;
  * отвечаем ТОЛЬКО ботам и шаблонным рассылкам (sender bot/template) — живому
    человеку пишет живой человек;
  * только kind=QUESTION: скрининг/редирект уводят на внешние формы, отвечать в чат
    бессмысленно; manual_only (деньги/место) в отправку не идёт даже с --send —
    показывается с пометкой, решение за человеком;
  * повторно в один чат не пишем: журнал data/chat_replies.jsonl + «последнее слово
    за нами» из analyze (после синка).
"""
import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from random import uniform
from typing import Any

from hrwork.application.apply.chat import chat, chat_answer, chat_class
from hrwork.application.apply.chat.chat_answer import VacancyContext
from hrwork.application.apply.runtime.store import store
from hrwork.config import DATA_DIR, log

REPLIES_LOG = DATA_DIR / "chat_replies.jsonl"
SEND_PAUSE = (2.0, 4.0)          # сек между отправками — не строчить как автомат
# Правила ответа, которые НИКОГДА не уходят автоматически — только под [y/N], как деньги/место.
# confirm («Используем эти ответы?»): бот подтверждает утверждения, которые проставил САМ
# (напр. «русский C1»), а не наши ответы. «Да, всё верно» вслепую заверил бы чужие формулировки,
# которых мы не видели (найдено на живом чате 21.07). Подтверждать можно, только глядя на них.
MANUAL_RULES = frozenset({"confirm"})
POLL_WINDOW_S = 300              # сколько ждём ответа бота после отправки (5 мин)
POLL_INTERVAL_S = 60             # как часто опрашиваем чат (раз в минуту)


@dataclass(frozen=True)
class Proposal:
    """Что готовы отправить в один чат (или показать в dry-run)."""
    vid: str
    chat_id: int
    question: str        # ПОЛНЫЙ текст последнего сообщения (не preview)
    text: str            # предлагаемый ответ
    rule: str
    lang: str
    sender: str          # bot | template
    manual: bool         # деньги/место: показать, но не отправлять автоматически


def _answered() -> set[str]:
    """Пары «чат + нормализованный вопрос», на которые уже отвечали.

    Дедуп ПО ВОПРОСУ, а не по чату: бот-интервьюер ведёт цепочку («…Следующий
    вопрос: …»), и на каждый новый вопрос в том же чате ответить нужно. Дедуп по
    vid запирал бы диалог после первого ответа. Ключ норм-текстом снимает
    подстановки (имя, ссылки), как в детекторе шаблонов.
    """
    if not REPLIES_LOG.exists():
        return set()
    out: set[str] = set()
    for line in REPLIES_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            out.add(f"{r['vid']}|{chat_class.norm_text(r.get('q') or '')}")
        except (ValueError, KeyError):
            continue                      # битая строка журнала не роняет чтение
    return out


def _answer_key(vid: str, question: str) -> str:
    return f"{vid}|{chat_class.norm_text(question)}"


def _ctx_map() -> dict[str, VacancyContext]:
    """id вакансии -> контекст для вилки по грейду. Вакансий, выпавших из выдачи,
    в кеше нет — их вопросы про деньги останутся без ответа (молчание, не ошибка)."""
    from hrwork.infrastructure.storage import vacancy_repository
    out: dict[str, VacancyContext] = {}
    for r in vacancy_repository().load():
        v = r.vacancy
        out[v.id] = VacancyContext(
            name=v.name, experience=v.experience.value if v.experience else "",
            city=v.city or "")
    return out


def propose(chats: dict[str, Any], ctx_map: dict[str, VacancyContext],
            answered: frozenset[str] | set[str] = frozenset(),
            classify: Callable[[str], Any] | None = None) -> list[Proposal]:
    """Чистая сборка предложений (тестируется без диска и сети).

    answered — ключи «vid|норм-вопрос», на которые уже отвечали (см. _answered):
    дедуп по конкретному вопросу, чтобы не повторяться, но и не запирать цепочку
    вопросов бота в одном чате.

    classify — опциональный LLM-классификатор намерения (inject; дефолт None -> сеть
    не дёргается, поведение = regex-путь suggest). Зовётся ТОЛЬКО на вопросах, реально
    дошедших до suggest (kind=QUESTION, needs_reply, не дедуп, bot/template) — объём мал.
    """
    templates = chat_class.build_template_index(chats)
    out: list[Proposal] = []
    for vid, d in chats.items():
        msgs = d.get("messages") or []
        a = chat_class.analyze(msgs, templates, d.get("write"))
        if a["kind"] != chat_class.ChatKind.QUESTION.code:
            continue
        if not (a["needs_reply"] and a.get("can_write")):
            continue
        if a["sender"] not in ("bot", "template"):
            continue                      # людям отвечает человек
        question = (msgs[-1].get("text") or "").strip()
        if _answer_key(vid, question) in answered:
            continue                      # на ЭТОТ вопрос уже отвечали
        intent = classify(question) if classify else None
        ans = chat_answer.suggest(question, ctx=ctx_map.get(vid), intent=intent)
        if not ans:
            continue                      # нет факта — вопрос остаётся человеку
        # manual — если вопрос про деньги/место (chat_class) ИЛИ правило ответа в MANUAL_RULES
        manual = bool(a.get("manual_only")) or ans["rule"] in MANUAL_RULES
        out.append(Proposal(
            vid=vid, chat_id=int(d.get("chatId") or 0), question=question,
            text=ans["text"], rule=ans["rule"], lang=ans["lang"],
            sender=a["sender"], manual=manual))
    return out


def poll_replies(req: Any, xsrf: str, vids: Sequence[str], *, window_s: int = POLL_WINDOW_S,
                 interval_s: int = POLL_INTERVAL_S,
                 sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Точечный опрос ТОЛЬКО указанных чатов после отправки — дёшево (O(len(vids)),
    не 600 чатов полного синка).

    Раз в interval_s проверяем каждый чат; как только по нему пришёл ответ бота
    (последнее слово НЕ наше) — снимаем с опроса и сохраняем свежую переписку в
    chat_messages.json (merge, не перезапись). Через window_s останавливаемся.

    sleep/clock инъектируются -> тестируется без реального ожидания.
    """
    pending: dict[str, tuple[Any, Any]] = {}
    for vid in vids:
        cid, aid = chat.find_chat(req, xsrf, vid)
        if cid:
            pending[vid] = (cid, aid)
        else:
            log.warning("poll: чат вакансии {} не найден", vid)
    if not pending:
        return {"answered": 0, "timeout": 0, "updated": 0}

    updates: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()
    start = clock()
    while pending and (clock() - start) < window_s:
        sleep(interval_s)
        for vid in list(pending):
            cid, aid = pending[vid]
            entry = chat.chat_entry(cid, chat.chat_data(req, xsrf, cid, aid))
            if not entry["messages"]:
                continue                          # пустой ответ (сбой/протухла) — ждём дальше
            updates[vid] = entry                  # сохраним последний снимок даже без ответа
            if not entry["messages"][-1]["mine"]:  # последнее слово за собеседником — ответил
                answered.add(vid)
                del pending[vid]
                log.success("poll: вакансия {} — пришёл ответ", vid)

    if updates:                                   # merge, а не перезапись всего файла
        cur = store.chat_messages()
        cur.update(updates)
        store.save_chat_messages(cur)
    log.info("poll: ответили {}/{}, без ответа за {} мин: {}",
             len(answered), len(vids), window_s // 60, len(pending))
    return {"answered": len(answered), "timeout": len(pending), "updated": len(updates)}


def _console_consent(p: "Proposal") -> bool:
    """Интерактивное подтверждение отправки manual-ответа (деньги/место).
    Без tty (крон/фон/пайп) — ОТКАЗ: вилка не должна уйти молча в неинтерактивной среде."""
    import sys
    if not (sys.stdin and sys.stdin.isatty()):
        log.warning("manual #{} [{}] — пропущен: нет интерактивного терминала "
                    "(отправка деньги/место требует подтверждения)", p.vid, p.rule)
        return False
    print(f"\n[MANUAL {p.rule}] вакансия {p.vid}\n  ВОПРОС: {p.question}\n  ОТВЕТ : {p.text}")
    return input("  Отправить этот ответ? [y/N] ").strip().lower() in ("y", "yes", "д", "да")


def select_targets(sendable: list["Proposal"], manual: list["Proposal"], *,
                   include_manual: bool,
                   consent: Callable[["Proposal"], bool]) -> list["Proposal"]:
    """Кто реально уйдёт: авто — все; manual — только при include_manual И согласии.
    Чистая (consent инъектируется) -> тестируется без ввода. Порядок сохранён."""
    out = list(sendable)
    if include_manual:
        out += [p for p in manual if consent(p)]
    return out


def _make_classifier() -> Callable[[str], Any] | None:
    """LLM-классификатор намерения с кэшем на прогон (один вопрос — один вызов сети),
    либо None если LLM выключена. Кэш по norm_text: переформулировки бота, сводимые к
    одному ключу, не дёргают сеть повторно."""
    from hrwork.application.apply.chat import chat_intent
    if not chat_intent.INTENT_ENABLED:
        return None
    cache: dict[str, Any] = {}

    def classify(question: str) -> Any:
        k = chat_class.norm_text(question)
        if k not in cache:
            cache[k] = chat_intent.classify_intent(question)
        return cache[k]
    return classify


def _make_rephraser() -> Callable[[str, str, str, str], str] | None:
    """LLM-переформулировщик одобренного факта под вопрос, либо None если выключено (etap-2).
    Кэш на прогон по (rule, норм-вопрос): один вопрос -> один вызов сети."""
    from hrwork.application.apply.chat import chat_rephrase
    if not chat_rephrase.REPHRASE_ENABLED:
        return None
    cache: dict[tuple[str, str], str] = {}

    def rephrase(question: str, source: str, rule: str, lang: str) -> str:
        k = (rule, chat_class.norm_text(question))
        if k not in cache:
            cache[k] = chat_rephrase.rephrase_answer(question, source, rule, lang)
        return cache[k]
    return rephrase


def _rephrased(p: "Proposal", rephrase: Callable[[str, str, str, str], str] | None) -> str:
    """Текст предложения после переформулировки (или исходный, если rephrase off / rule не eligible)."""
    from hrwork.application.apply.chat.chat_rephrase import eligible
    if not (rephrase and eligible(p.rule)):
        return p.text
    return rephrase(p.question, p.text, p.rule, p.lang)


def _rephrase_consent(p: "Proposal", cand: str) -> bool:
    """Подтверждение ИЗМЕНЁННОЙ переформулировки: человек видит источник и кандидата.
    Без tty (крон/фон/пайп) — ОТКАЗ: изменённый текст не уходит без просмотра -> шлём источник."""
    import sys
    if not (sys.stdin and sys.stdin.isatty()):
        log.info("rephrase #{} [{}] изменён, но нет tty — шлём ИСТОЧНИК дословно", p.vid, p.rule)
        return False
    print(f"\n[REPHRASE {p.rule}] вакансия {p.vid}\n  ВОПРОС    : {p.question}"
          f"\n  ИСТОЧНИК  : {p.text}\n  КАНДИДАТ  : {cand}")
    return input("  Отправить переформулировку? [y/N] ").strip().lower() in ("y", "yes", "д", "да")


def _apply_rephrase(props: list["Proposal"],
                    rephrase: Callable[[str, str, str, str], str] | None,
                    consent: Callable[..., bool] = _rephrase_consent) -> list["Proposal"]:
    """Переформулировать предложения перед отправкой: не eligible/без изменений -> как есть
    (авто); ИЗМЕНЁН -> под consent (без tty -> источник). Proposal заморожен -> replace."""
    out: list[Proposal] = []
    for p in props:
        cand = _rephrased(p, rephrase)
        if cand == p.text:
            out.append(p)                       # не eligible / без изменений -> авто как сегодня
        elif consent(p, cand):
            out.append(replace(p, text=cand))   # человек одобрил переформулировку
        else:
            out.append(p)                       # отказ / крон -> источник дословно
    return out


def _plan(only: str, limit: int,
          classify: Callable[[str], Any] | None = None) -> tuple[list["Proposal"], list["Proposal"]]:
    """Свежий снимок с диска -> (sendable, manual). Читается КАЖДЫЙ раунд: poll обновил
    chat_messages новыми вопросами, журнал дописан -> дедуп и вопросы актуальны."""
    props = propose(store.chat_messages(), _ctx_map(), _answered(), classify=classify)
    if only:
        props = [p for p in props if p.vid == str(only)]
    sendable = [p for p in props if not p.manual][:limit]
    manual = [p for p in props if p.manual]
    return sendable, manual


def _send_batch(req: Any, xsrf: str, targets: list["Proposal"]) -> list[str]:
    """Отправить пачку; вернуть vid успешно отправленных. Журналирует каждый (дедуп по q)."""
    sent: list[str] = []
    for p in targets:
        key = str(uuid.uuid4())
        ok = chat.send_message(req, xsrf, p.chat_id, p.text,
                               vacancy_url=f"https://hh.ru/vacancy/{p.vid}",
                               idempotency_key=key)
        if ok:
            sent.append(p.vid)
            with REPLIES_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"vid": p.vid, "chat_id": p.chat_id, "rule": p.rule,
                                    "q": p.question, "text": p.text, "key": key,
                                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False) + "\n")
            log.success("Отправлено #{} [{}]", p.vid, p.rule)
        else:
            log.warning("Не отправилось #{} (протухла сессия / чат закрылся?)", p.vid)
        time.sleep(uniform(*SEND_PAUSE))
    return sent


def run(send: bool = False, limit: int = 10, only: str = "", wait: bool = True,
        include_manual: bool = False, consent: Callable[..., bool] = _console_consent,
        max_rounds: int = 1, use_intent: bool = False, use_rephrase: bool = False) -> dict[str, Any]:
    """Точка входа CLI. dry-run (по умолчанию) печатает предложения; --send отправляет
    не-manual через cookie-сессию (браузер не нужен).

    max_rounds > 1 (--loop) — ВЕДЁТ ДИАЛОГ до конца: после каждого ответа poll ждёт
    следующий вопрос бота, и цикл отвечает на него. Стоп, когда отвечать нечего
    (бот прислал не-вопрос / нет факта / только manual) или бот замолчал — тупик уходит
    человеку (решение владельца профиля). max_rounds — предохранитель от зацикливания.

    use_intent (--intent) — маршрутизировать вопросы кластера через LLM-классификатор
    (если INTENT_LLM=1). None -> regex-путь как раньше.

    only — id вакансии: отправить ТОЛЬКО в этот чат.
    """
    classify = _make_classifier() if use_intent else None
    rephrase = _make_rephraser() if use_rephrase else None
    sendable, manual = _plan(only, limit, classify)
    if only and not sendable and not manual:
        log.warning("Для вакансии {} предложения нет (уже отвечали / нет факта / "
                    "не бот / чат закрыт)", only)
    for p in sendable:
        cand = _rephrased(p, rephrase)
        if cand != p.text:                                # dry-run показывает переформулировку
            log.info("[{}] #{}: «{}»\n    источник: «{}»\n    кандидат: «{}»",
                     p.rule, p.vid, p.question[:80], p.text, cand)
        else:
            log.info("[{}] {} #{}: «{}» -> «{}»", p.rule, p.sender, p.vid,
                     p.question[:80], p.text)
    for p in manual:
        tag = "manual, спросим" if include_manual else "manual, НЕ отправляется"
        log.info("[{}] #{}: «{}» -> предложение: «{}»",
                 tag, p.vid, p.question[:80], p.text)
    if not send:
        log.info("DRY-RUN: {} готово к отправке, {} manual{}. Отправка: --send",
                 len(sendable), len(manual),
                 " (уйдут с подтверждением при --include-manual)" if not include_manual else "")
        return {"proposed": len(sendable), "manual": len(manual), "sent": 0}

    from hrwork.application.apply.session import open_client
    req, xsrf = open_client()
    if req is None:
        log.error("Нет сессии (hh_state.json) — сначала браузерный прогон/--login")
        return {"proposed": len(sendable), "manual": len(manual), "sent": 0}

    all_sent: list[str] = []
    answered_vids: set[str] = set()      # чат, отвеченный в ЭТОМ прогоне — второй раз не трогаем
    rounds = 0
    with req:
        while rounds < max(1, max_rounds):
            rounds += 1
            if rounds > 1:                                  # раунды 2+ — свежий план после poll
                sendable, manual = _plan(only, limit, classify)
            send_list = _apply_rephrase(sendable, rephrase) if rephrase else sendable
            targets = select_targets(send_list, manual,
                                     include_manual=include_manual, consent=consent)
            # ANTI-LOOP: один чат — один ответ за прогон. Бот-интервьюер может переспрашивать,
            # чуть меняя формулировку (дедуп по вопросу её не ловит), и loop долбил бы один
            # чат впустую (БАГ 21.07: 5 отправок про «стаж с React»). Реальное продолжение
            # диалога подхватит СЛЕДУЮЩИЙ прогон hh_chat через 90 мин.
            loop_targets = [p for p in targets if p.vid not in answered_vids]
            if len(loop_targets) < len(targets):
                skipped = {p.vid for p in targets} - {p.vid for p in loop_targets}
                log.info("anti-loop: чат(ы) {} уже отвечены в этом прогоне — пропуск "
                         "(бот переспрашивает?)", ", ".join(sorted(skipped)))
            if not loop_targets:
                if rounds > 1:
                    log.info("Диалоги исчерпаны/зациклены: новых чатов нет (раунд {})", rounds)
                break
            sent = _send_batch(req, xsrf, loop_targets)
            answered_vids.update(sent)
            all_sent += sent
            if not (wait and sent):
                break
            poll = poll_replies(req, xsrf, sent)
            if poll["answered"] == 0:                       # бот не задал новых вопросов
                break
        log.info("Итог: отправлено {} за {} раунд(ов); manual в очереди: {}",
                 len(all_sent), rounds, len(manual))
    return {"sent": len(all_sent), "rounds": rounds, "manual": len(manual)}
