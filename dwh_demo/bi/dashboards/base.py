"""Контракт дашборда + хелперы раскладки."""
from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from ..client import MetabaseClient


@runtime_checkable
class Dashboard(Protocol):
    """Контракт дашборда. Соответствие — структурное (без наследования):
    класс «является» Dashboard, если у него есть key, title и build().
    @runtime_checkable включает проверку через isinstance (см. тест)."""

    key: str       # позиционное имя для CLI (`python -m bi <key>`)
    title: str     # заголовок в Metabase

    def build(self, client: MetabaseClient) -> None: ...


# ── словарь подписей: одно понятие — одна формулировка на все дашборды ──
# Подпись обязана называть то, что метрика СЧИТАЕТ. Оба текста ниже появились 09.08.2026
# после того, как аудит нашёл на дашбордах тот же класс дефекта, что «Автобан %» с двумя
# знаменателями у родителя: разные карточки обещали одно, а считали другое.

# Витрины считают удалёнку по `is_remote` = коды remote + flexible
# (`etl/domain.py::REMOTE_LIKE_CODES`, зеркало `Schedule.is_remote_like` родителя).
# Это НЕ «строго удалённо»: гибрид входит, и подпись обязана это говорить.
REMOTE_LIKE = "Удалённо или гибрид"

# Суммы в витринах агрегируются В ВАЛЮТЕ ПОРТАЛА (в кеше 45 кодов: RUB, USD, EUR, KZT,
# UZS…), без приведения к одной шкале. Пока витрины не отдают рублёвый эквивалент,
# столбики РАЗНЫХ источников/движков сравнивать нельзя, и подпись называет единицу.
MIXED_CURRENCY = " · валюта портала (суммы не сравнимы)"


def layout(items: list) -> list:
    """items: (card_id, row, col, w, h[, parameter_mappings]) -> dashcards с уникальными id."""
    out = []
    for i, it in enumerate(items, 1):
        cid, row, col, w, h = it[:5]
        mappings = it[5] if len(it) > 5 else []
        out.append({
            "id": -i, "card_id": cid, "row": row, "col": col,
            "size_x": w, "size_y": h,
            "parameter_mappings": mappings, "visualization_settings": {},
        })
    return out


def text_tag(name: str, display: str, **extra) -> dict:
    """Metabase template-tag. Опциональные поля (required, default, …) — через **extra:
    вызывающий передаёт только нужные ключи, без флаг-аргументов и условий."""
    return {name: {
        "id": str(uuid.uuid4()), "name": name, "display-name": display,
        "type": "text", **extra,
    }}


def bar(dim: str, *metrics: str) -> dict:
    return {"graph.dimensions": [dim], "graph.metrics": list(metrics)}
