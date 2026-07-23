"""Ревью автоответов: вопрос бота -> наш ответ -> РЕАКЦИЯ бота. Read-only.

Зачем: chat_replies.jsonl хранит что мы отправили, но не как бот отреагировал.
Реакция (следующее сообщение бота после нашего) — сигнал качества шаблона:
принял и пошёл дальше / переспросил / отказал. Скрипт сводит это в одну ленту,
подсвечивает подозрительное (отказ или повтор того же вопроса после ответа) —
чтобы понять, какие формулировки/факты стоит поправить.

Запуск:  .venv3/Scripts/python.exe scripts/chat_review.py [--bad] [--rule RULE]
  --bad        только проблемные (реакция = отказ / переспрос)
  --rule NAME  только ответы этого правила (напр. practice_ml)
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hrwork.application.apply import chat_class  # noqa: E402

REPLIES = ROOT / "data" / "chat_replies.jsonl"
CHATS = ROOT / "data" / "chat_messages.json"


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _reaction(messages: list[dict], our_text: str) -> tuple[str, str]:
    """Найти наш ответ в переписке и вернуть (тип_реакции, текст) следующего сообщения бота.
    Типы: 'ждём' (мы последние), 'reject', 'repeat' (тот же вопрос снова), 'дальше' (новый),
    'invite'."""
    target = _norm(our_text)[:80]
    idx = next((i for i, m in enumerate(messages)
                if m.get("mine") and _norm(m.get("text") or "").startswith(target[:40])), None)
    if idx is None:
        return "нет в переписке", ""
    after = [m for m in messages[idx + 1:] if (m.get("text") or "").strip() and not m.get("mine")]
    if not after:
        return "ждём ответа", ""
    react = after[0].get("text") or ""
    kind = chat_class.classify(react)
    if kind is chat_class.ChatKind.REJECT:
        return "❌ отказ", react
    if kind is chat_class.ChatKind.INVITE:
        return "🎉 приглашение", react
    # тот же вопрос ещё раз (бот не принял ответ)?
    prev_q = next((m.get("text") for m in reversed(messages[:idx]) if not m.get("mine")), "")
    if _norm(react)[:60] == _norm(prev_q)[:60] and prev_q:
        return "🔁 переспросил", react
    return "➡ дальше", react


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="chat_review")
    ap.add_argument("--bad", action="store_true", help="только проблемные (отказ/переспрос)")
    ap.add_argument("--rule", default="", help="фильтр по правилу ответа")
    args = ap.parse_args(argv)

    if not REPLIES.exists():
        print("Нет data/chat_replies.jsonl — автоответов ещё не было.")
        return
    chats = json.loads(CHATS.read_text(encoding="utf-8")) if CHATS.exists() else {}
    replies = [json.loads(x) for x in REPLIES.read_text(encoding="utf-8").splitlines() if x.strip()]

    stats: dict[str, int] = {}
    shown = 0
    for r in replies:
        if args.rule and r.get("rule") != args.rule:
            continue
        msgs = (chats.get(str(r["vid"])) or {}).get("messages") or []
        react_kind, react_text = _reaction(msgs, r["text"])
        stats[react_kind] = stats.get(react_kind, 0) + 1
        bad = react_kind in ("❌ отказ", "🔁 переспросил")
        if args.bad and not bad:
            continue
        shown += 1
        print(f"\n{'━'*70}\nчат {r['vid']}  [{r['rule']}]  {r.get('ts','')}")
        print(f"  ВОПРОС: {(r.get('q') or '(не сохранён)')[:150]}")
        print(f"  ОТВЕТ : {r['text'][:150]}")
        print(f"  РЕАКЦИЯ [{react_kind}]: {react_text[:150]}")

    print(f"\n{'═'*70}\nвсего автоответов: {len(replies)}  показано: {shown}")
    print("реакции бота:", ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    bad_n = stats.get("❌ отказ", 0) + stats.get("🔁 переспросил", 0)
    if bad_n:
        print(f"⚠ проблемных (отказ/переспрос): {bad_n} — стоит глянуть формулировки: "
              f"chat_review.py --bad")


if __name__ == "__main__":
    main()
