"""Чтение полей формы-анкеты HH через Playwright (RFC-003). ТОЛЬКО ЧТЕНИЕ DOM — ничего не
заполняет и не отправляет. Всё в contextlib.suppress: флэки-DOM не роняет прогон.

Реальная HH task-форма (выверено живым прогоном): каждый `[data-qa="task-body"]` = вопрос
`[data-qa="task-question"]` + группа выбора radio/checkbox (`input[name="task_N"]`) с подписями
в `[data-qa="cell"]`; «Свой вариант» = опция value='open' + парный `textarea[name="task_N_text"]`.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_MAX_FIELDS = 40         # разумный потолок анкеты
_MAX_PROMPT = 1000       # усечение текста вопроса — сужение поверхности инъекции


class FieldType(Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"

    @property
    def code(self) -> str:
        return self.value


@dataclass(frozen=True)
class FormField:
    selector: str
    name: str
    prompt: str
    ftype: FieldType
    options: tuple[str, ...] = field(default_factory=tuple)     # подписи вариантов (radio/checkbox/select)
    opt_values: tuple[str, ...] = field(default_factory=tuple)  # value для каждой подписи (отметка по value)


def _clip(text: str) -> str:
    return " ".join((text or "").split())[:_MAX_PROMPT]


def _attr(loc: Any, name: str) -> str:
    with contextlib.suppress(Exception):
        return loc.get_attribute(name) or ""
    return ""


def _text(loc: Any) -> str:
    with contextlib.suppress(Exception):
        return loc.inner_text() or ""
    return ""


def extract_fields(page: Any) -> list[FormField]:
    """Все заполняемые поля анкеты -> [FormField]. Дедуп по selector, лимит кол-ва."""
    out: list[FormField] = []
    seen: set[str] = set()
    for f in (*_tasks(page), *_selects(page)):
        if not f or f.selector in seen:
            continue
        seen.add(f.selector)
        out.append(f)
        if len(out) >= _MAX_FIELDS:
            break
    return out


def _tasks(page: Any) -> list[FormField]:
    """HH task-форма: [data-qa=task-body] -> вопрос + группа radio/checkbox с подписями-cell.
    Подписи (Да/Нет/1/Свой вариант) в options, value каждой — в opt_values (для отметки)."""
    out: list[FormField] = []
    with contextlib.suppress(Exception):
        for body in page.locator('[data-qa="task-body"]').all():
            q = _clip(_text(body.locator('[data-qa="task-question"]').first))
            radios = body.locator('input[type="radio"]').all()
            checks = body.locator('input[type="checkbox"]').all()
            inputs, ftype = (radios, FieldType.RADIO) if radios else (checks, FieldType.CHECKBOX)
            if not inputs:
                # задача со СВОБОДНЫМ ответом (textarea/text, без вариантов) — тоже обязательное поле
                ta = body.locator('textarea, input[type="text"]')
                if ta.count():
                    tn = _attr(ta.first, "name")
                    if tn:
                        out.append(FormField(selector=f'[name="{tn}"]', name=tn, prompt=q or tn,
                                             ftype=FieldType.TEXTAREA))
                continue
            name = _attr(inputs[0], "name")
            cells = [t for t in (_text(c) for c in body.locator('[data-qa="cell"]').all()) if t]
            labels = [cells[i] if i < len(cells) else _attr(inp, "value") for i, inp in enumerate(inputs)]
            values = [_attr(inp, "value") for inp in inputs]
            out.append(FormField(selector=f'input[name="{name}"]', name=name, prompt=q or name,
                                 ftype=ftype, options=tuple(labels), opt_values=tuple(values)))
    return out


def _selects(page: Any) -> list[FormField]:
    """Обычные <select> — на случай анкет не в HH-task-формате."""
    out: list[FormField] = []
    with contextlib.suppress(Exception):
        for sel in page.locator("select").all():
            name = _attr(sel, "name")
            if not name:
                continue
            opts = tuple(o for o in (_text(o) for o in sel.locator("option").all()) if o.strip())
            out.append(FormField(selector=f'select[name="{name}"]', name=name,
                                 prompt=_field_prompt(page, sel, name), ftype=FieldType.SELECT, options=opts))
    return out


def _field_prompt(page: Any, loc: Any, name: str) -> str:
    """Промпт поля: <label for=id> / aria-label / placeholder. Пусто -> имя поля."""
    with contextlib.suppress(Exception):
        fid = _attr(loc, "id")
        if fid:
            lbl = page.locator(f'label[for="{fid}"]')
            if lbl.count():
                return _clip(_text(lbl.first))
    for a in ("aria-label", "placeholder"):
        v = _attr(loc, a)
        if v.strip():
            return _clip(v)
    return name
