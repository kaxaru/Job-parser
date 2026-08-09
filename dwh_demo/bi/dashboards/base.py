"""Контракт дашборда + хелперы раскладки."""
from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

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

# Единица измерения В ПОДПИСИ КАРТОЧКИ, а не только в имени колонки: человек, открывший
# дашборд, видит заголовок и столбики, а не SQL. С 09.08.2026 витрины отдают рублёвый
# эквивалент (`*_rub`, конверсия по суточному кешу курсов делается один раз в домене),
# поэтому суммы сравнимы между источниками и движками — но сказать об этом обязана подпись.
RUB = " · ₽ (пересчёт по курсу)"

# Осталось для карточек, которые всё ещё считают по СЫРЫМ суммам. В кеше 45 кодов валют
# (RUB, USD, EUR, KZT, UZS…), и без приведения к одной шкале столбики разных источников
# сравнивать нельзя. Новую карточку с этим маркером заводить не надо — надо брать `*_rub`.
MIXED_CURRENCY = " · валюта портала (суммы не сравнимы)"


def layout(items: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """items: (card_id, row, col, w, h[, parameter_mappings]) -> dashcards с уникальными id."""
    # Кортеж разнородный и переменной длины (5 или 6), поэтому `tuple[Any, ...]`:
    # позиционный формат раскладки задан докстрингом, а не типом.
    out: list[dict[str, Any]] = []
    for i, it in enumerate(items, 1):
        cid, row, col, w, h = it[:5]
        mappings = it[5] if len(it) > 5 else []
        out.append({
            "id": -i, "card_id": cid, "row": row, "col": col,
            "size_x": w, "size_y": h,
            "parameter_mappings": mappings, "visualization_settings": {},
        })
    return out


def text_tag(name: str, display: str, **extra: Any) -> dict[str, dict[str, Any]]:
    """Metabase template-tag. Опциональные поля (required, default, …) — через **extra:
    вызывающий передаёт только нужные ключи, без флаг-аргументов и условий."""
    return {name: {
        "id": str(uuid.uuid4()), "name": name, "display-name": display,
        "type": "text", **extra,
    }}


def bar(dim: str, *metrics: str) -> dict[str, list[str]]:
    return {"graph.dimensions": [dim], "graph.metrics": list(metrics)}
