"""Текстовые и типовые утилиты адаптеров источников.

Отдельный модуль, а не приватная функция внутри `hh.py`: тот же стриппер нужен getmatch,
и импорт `hh._strip_html` соседним адаптером связал бы два источника друг с другом ради
одной регулярки (так уже вышло с `BROWSER_UA`).
"""
import re
from typing import Any

from hrwork.domain.freshness import parse_dt

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """HTML описания -> плоский текст: теги в пробелы, пробелы схлопнуты.

    Теги заменяются ПРОБЕЛОМ, а не пустой строкой: `<li>Python</li><li>Go</li>` иначе дал бы
    склейку `PythonGo`, и ни один паттерн стека или формы оформления её бы не нашёл."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", s)).strip()


def _iso_utc(value: Any) -> str | None:
    """Метка времени портала -> ISO-UTC; мусор -> None.

    Разбор — доменный `freshness.parse_dt`: он уже понимает и unix-секунды, и ISO с «Z», и
    наивную метку (додумывает UTC). Поэтому пять адаптерных копий `_iso` были не пятью
    правилами, а одним — с РАЗНОЙ шириной `except` (у unix-копий `OSError`, у ISO-копий
    только `ValueError`): наивная метка у одной копии оставалась без таймзоны, а у другой
    получала UTC. Ширина `except` теперь одна — доменная.
    """
    dt = parse_dt(value)
    return dt.isoformat() if dt is not None else None


def ts_to_iso(ts: Any) -> str | None:
    """Unix-секунды портала -> ISO-UTC (arbeitnow, himalayas, web3.career)."""
    return _iso_utc(ts)


def iso_to_iso(raw: Any) -> str | None:
    """ISO-строка портала -> ISO-UTC (jobicy, themuse)."""
    return _iso_utc(raw)
