"""Чтение полей формы-анкеты HH через Playwright (RFC-003). ТОЛЬКО ЧТЕНИЕ DOM — ничего не
заполняет и не отправляет. Флэки-DOM не роняет прогон, но гасится ПОИТЕРАЦИОННО: сбой на
одном вопросе не обрывает съём остальных и не проходит молча — он считается в
`FormExtract.missed`, а отправку гейтит `forms.py::try_autofill`.

Реальная HH task-форма (выверено живым прогоном): каждый `[data-qa="task-body"]` = вопрос
`[data-qa="task-question"]` + группа выбора radio/checkbox (`input[name="task_N"]`) с подписями
в `[data-qa="cell"]`; «Свой вариант» = опция value='open' + парный `textarea[name="task_N_text"]`.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hrwork.config import log

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


@dataclass(frozen=True)
class FormExtract:
    """Результат съёма анкеты: поля + сколько вопросов снять НЕ удалось.

    Полнота СЪЁМА — такая же часть инварианта «шлём ТОЛЬКО при полноте» (RFC-003, риск b),
    как полнота резолва и полнота заполнения. До 08.08.2026 весь цикл извлечения лежал под
    ОДНИМ `contextlib.suppress`: исключение на пятом вопросе возвращало четыре поля, дальше
    `forms.py::try_autofill` резолвил 4 из 4 и считал анкету закрытой. Гейт полноты
    формально проходил ПО ОБРЕЗАННОМУ СПИСКУ, и отклик уходил с дырами — ловила только
    валидация HH, причём причина в логах не была видна."""
    fields: tuple[FormField, ...]
    missed: int = 0

    @property
    def complete(self) -> bool:
        """Все вопросы страницы сняты — только тогда анкету вообще можно отправлять."""
        return self.missed == 0


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


def extract_form(page: Any) -> FormExtract:
    """Поля анкеты + признак ПОЛНОТЫ съёма. Дедуп по selector, лимит кол-ва.

    Дедуп и потолок тоже считаются несъёмом: два вопроса на один селектор нечем заполнить
    по отдельности, а хвост анкеты длиннее `_MAX_FIELDS` мы вообще не видели."""
    task_fields, missed = _tasks(page)
    select_fields, missed_selects = _selects(page)
    missed += missed_selects
    out: list[FormField] = []
    seen: set[str] = set()
    for f in (*task_fields, *select_fields):
        if f.selector in seen or len(out) >= _MAX_FIELDS:
            missed += 1
            continue
        seen.add(f.selector)
        out.append(f)
    return FormExtract(fields=tuple(out), missed=missed)


def extract_fields(page: Any) -> list[FormField]:
    """Только поля, без признака полноты — для свипа структуры и `--dry`-превью, где
    полнота ничего не гейтит. Боевой путь отправки берёт `extract_form`."""
    return list(extract_form(page).fields)


def _task_field(body: Any) -> FormField | None:
    """Один `[data-qa=task-body]` -> поле анкеты. None — контрол не распознан (вопрос есть,
    заполнять нечего): для отправки это такой же пробел, как несработавший селектор."""
    q = _clip(_text(body.locator('[data-qa="task-question"]').first))
    radios = body.locator('input[type="radio"]').all()
    checks = body.locator('input[type="checkbox"]').all()
    inputs, ftype = (radios, FieldType.RADIO) if radios else (checks, FieldType.CHECKBOX)
    if not inputs:
        # задача со СВОБОДНЫМ ответом (textarea/text, без вариантов) — тоже обязательное поле
        ta = body.locator('textarea, input[type="text"]')
        tn = _attr(ta.first, "name") if ta.count() else ""
        if not tn:
            return None
        return FormField(selector=f'[name="{tn}"]', name=tn, prompt=q or tn,
                         ftype=FieldType.TEXTAREA)
    name = _attr(inputs[0], "name")
    cells = [t for t in (_text(c) for c in body.locator('[data-qa="cell"]').all()) if t]
    labels = [cells[i] if i < len(cells) else _attr(inp, "value") for i, inp in enumerate(inputs)]
    values = [_attr(inp, "value") for inp in inputs]
    return FormField(selector=f'input[name="{name}"]', name=name, prompt=q or name,
                     ftype=ftype, options=tuple(labels), opt_values=tuple(values))


def _tasks(page: Any) -> tuple[list[FormField], int]:
    """HH task-форма: [data-qa=task-body] -> вопрос + группа radio/checkbox с подписями-cell.
    Подписи (Да/Нет/1/Свой вариант) в options, value каждой — в opt_values (для отметки).
    -> (поля, число НЕ снятых вопросов).

    Ошибка гасится НА ИТЕРАЦИЮ: соседние вопросы снимаются дальше, а несъём попадает
    в счётчик — вместо тихой обрезки списка (см. `FormExtract`)."""
    out: list[FormField] = []
    try:
        bodies = page.locator('[data-qa="task-body"]').all()
    except Exception as e:
        log.warning("form_read: список вопросов не прочитан ({}) — съём неполон", repr(e)[:120])
        return out, 1                     # сколько их было, неизвестно: анкета заведомо неполна
    missed = 0
    for body in bodies:
        try:
            f = _task_field(body)
        except Exception as e:
            log.warning("form_read: вопрос не снят ({}) — анкета неполна", repr(e)[:120])
            f = None
        if f is None:
            missed += 1
        else:
            out.append(f)
    return out, missed


def _selects(page: Any) -> tuple[list[FormField], int]:
    """Обычные <select> — на случай анкет не в HH-task-формате. -> (поля, не снятые)."""
    out: list[FormField] = []
    try:
        sels = page.locator("select").all()
    except Exception as e:
        log.warning("form_read: список select не прочитан ({}) — съём неполон", repr(e)[:120])
        return out, 1
    missed = 0
    for sel in sels:
        try:
            name = _attr(sel, "name")
            if not name:
                continue          # безымянный <select> — виджет страницы, а не поле анкеты
            opts = tuple(o for o in (_text(o) for o in sel.locator("option").all()) if o.strip())
            out.append(FormField(selector=f'select[name="{name}"]', name=name,
                                 prompt=_field_prompt(page, sel, name),
                                 ftype=FieldType.SELECT, options=opts))
        except Exception as e:
            log.warning("form_read: select не снят ({}) — анкета неполна", repr(e)[:120])
            missed += 1
    return out, missed


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
