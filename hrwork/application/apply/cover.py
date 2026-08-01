"""Сопроводительное письмо для автоотклика: шаблон (по умолчанию) или LLM.

Шаблон подставляет название вакансии и компанию. LLM (опционально, при заданном
ANTHROPIC_API_KEY) пишет письмо по описанию — при любой ошибке падает обратно на шаблон,
чтобы отклик всё равно ушёл. Чистые функции (шаблон тестируется без сети).
"""
import os
from typing import Any

from hrwork.config import log

COVER_TEMPLATE = ("Здравствуйте! Заинтересовала вакансия {name} в компании {employer}. "
                  "Буду рад обсудить детали.")
_LLM_MODEL = "claude-haiku-4-5-20251001"   # дёшево и быстро для коротких писем
_MAX_LETTER = 1800                          # HH режет длинные письма — подстрахуемся


def template_cover(name: str, employer: str) -> str:
    """Дефолтное письмо из шаблона (без сети). employer пустой -> «вашей компании»."""
    emp = (employer or "").strip() or "вашей компании"
    return COVER_TEMPLATE.format(name=(name or "").strip(), employer=emp)


def llm_cover(name: str, employer: str, description: str) -> str | None:
    """Письмо от Claude по описанию вакансии. None — если нет ключа/SDK/ошибка
    (вызывающий откатывается на шаблон)."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.debug("anthropic не установлен — письмо из шаблона")
        return None
    prompt = (
        "Напиши короткое (3–4 предложения) деловое сопроводительное письмо на русском "
        "для отклика на вакансию. Без «шаблонности», по-человечески, без воды и без "
        "выдуманных фактов обо мне. Только текст письма.\n\n"
        f"Вакансия: {name}\nКомпания: {employer or 'не указана'}\n"
        f"Описание: {(description or '')[:1500]}"
    )
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=_LLM_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return text[:_MAX_LETTER] or None
    except Exception as e:
        log.warning("LLM-письмо не сгенерировано ({}) — шаблон", e)
        return None


def build_cover(cand: Any, mode: str = "template") -> str:
    """Текст письма для кандидата (Candidate). mode='llm' пробует Claude и падает на шаблон;
    'template' (по умолчанию) — сразу шаблон."""
    if mode == "llm":
        txt = llm_cover(cand.name, cand.employer, cand.desc)
        if txt:
            return txt
    return template_cover(cand.name, cand.employer)
