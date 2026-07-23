"""Шаг 1 исследования автоответов: собрать ВСЕ сообщения из чатов HH и классифицировать.

Только ЧТЕНИЕ (тот же cookie-доступ, что у синка) — ничего не отправляется.
Сырые сообщения складываем в data/chat_messages.json, чтобы переклассифицировать
их потом офлайн, не обходя API заново.

Запуск:  .venv3/Scripts/python.exe scripts/chat_stats.py [limit]
"""
import collections
import io
import re
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # запуск из scripts/

from hrwork.application.apply import chat, session  # noqa: E402
from hrwork.config import DATA_DIR, log  # noqa: E402
from hrwork.infrastructure.storage import atomic_write_json, read_json_or  # noqa: E402

OUT_FILE = DATA_DIR / "chat_messages.json"

# ── Классификация сообщения работодателя ──
# Признаки собраны по живой выборке 19.07: боты уводят на ВНЕШНИЙ скрининг, а не спрашивают.
BOT_SCREENING = re.compile(
    r"первичн\w*\s+интервью|гигарекрутер|пройдите\s+(?:короткое|опрос)|"
    r"чтобы работодатель узнал|ответьте на (?:несколько )?вопрос|"
    r"не забудьте пройти|пройти отбор|видеоинтервью|тестовое задание по ссылке", re.I)
REJECT = re.compile(
    r"к сожалению|не готовы пригласить|отказ|не подходит|другого кандидата|"
    r"вакансия закрыта", re.I)
INVITE = re.compile(
    r"приглаша\w+|готовы предложить|назначим|созвон|собеседовани\w*\s+(?:в|на)|"
    r"когда вам удобно|буд(?:у|ем) рад обсудить", re.I)


def classify(text: str) -> str:
    t = " ".join((text or "").split())
    if not t:
        return "пусто"
    if BOT_SCREENING.search(t):
        return "бот: внешний скрининг/анкета"
    if REJECT.search(t):
        return "отказ"
    if INVITE.search(t):
        return "приглашение/интерес"
    if "?" in t:
        return "ПРЯМОЙ ВОПРОС (отвечаем)"
    return "прочее/живой текст"


def collect(limit: int | None = None) -> dict:
    req, xsrf = session.open_client()
    if req is None:
        log.error("Нет сессии ({}) — нужен браузерный прогон", session.STATE_FILE)
        return {}
    out: dict[str, dict] = read_json_or(OUT_FILE, {})
    with req:
        chats = chat.list_chats(req, xsrf)
        if limit:
            chats = chats[:limit]
        log.info("Чатов к обходу: {}", len(chats))
        for i, c in enumerate(chats, 1):
            vid = str(c["vacancyId"])
            d = chat.chat_data(req, xsrf, c["chatId"], c["applicantId"])
            items = (((d.get("chat") or {}).get("messages") or {}).get("items")) or []
            msgs = [{"text": m.get("text") or "", "mine": bool(m.get("canEdit")),
                     "ts": m.get("creationTime") or "", "type": m.get("type") or "",
                     "who": (m.get("participantDisplay") or {}).get("name")
                            if isinstance(m.get("participantDisplay"), dict) else None}
                    for m in items if (m.get("text") or "").strip()]
            out[vid] = {"chatId": c["chatId"], "state": chat.deep_get(d, "currentApplicantState"),
                        "messages": msgs}
            if i % 50 == 0:
                log.info("  …{}/{}", i, len(chats))
            time.sleep(0.12)
    atomic_write_json(OUT_FILE, out, indent=0)
    log.success("Сохранено чатов: {} -> {}", len(out), OUT_FILE)
    return out


def report(data: dict) -> None:
    total = len(data)
    with_reply = {v: d for v, d in data.items() if any(not m["mine"] for m in d["messages"])}
    print(f"\n{'=' * 66}")
    print(f"  ЧАТОВ ВСЕГО: {total}   |   С ОТВЕТОМ РАБОТОДАТЕЛЯ: {len(with_reply)} "
          f"({len(with_reply) * 100 // max(total, 1)}%)")
    print("=" * 66)

    theirs = [m["text"] for d in data.values() for m in d["messages"] if not m["mine"]]
    print(f"\nсообщений от работодателей: {len(theirs)}\n")

    cls = collections.Counter(classify(t) for t in theirs)
    print("--- РАСПРЕДЕЛЕНИЕ ПО ТИПАМ ---")
    for k, n in cls.most_common():
        print(f"  {n:5}  ({n * 100 // max(len(theirs), 1):3}%)  {k}")

    # дубликаты = машинная рассылка
    norm = [" ".join(t.split())[:120] for t in theirs]
    dup = collections.Counter(norm)
    repeated = sum(n for _t, n in dup.items() if n > 1)
    print(f"\n--- ПОВТОРЫ (признак бота): {repeated} из {len(theirs)} "
          f"({repeated * 100 // max(len(theirs), 1)}%) ---")
    for t, n in dup.most_common(10):
        if n > 1:
            print(f"  [{n:3}x] {t[:98]}")

    q = [t for t in theirs if classify(t) == "ПРЯМОЙ ВОПРОС (отвечаем)"]
    print(f"\n--- ПРЯМЫЕ ВОПРОСЫ (кандидаты на автоответ): {len(q)} ---")
    for t in q[:15]:
        print(f"  • {' '.join(t.split())[:100]}")


if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    report(collect(lim))
