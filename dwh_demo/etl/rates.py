"""Адаптер курсов валют: читает суточный кеш родителя `data/fx_rates.json`. БЕЗ СЕТИ.

Порт для домена — обычный `dict[str, float]` курсов per-USD; откуда он взялся, домен не
знает (`Vacancy.from_raw(rec, fx=...)`). Здесь — единственная реализация порта: файл,
который родитель обновляет сам (`hrwork/infrastructure/net/rates.py::get_rates`, TTL 24 ч).
Ходить за курсами в сеть стенд не имеет права: прогон витрин не должен зависеть от
доступности внешнего API и не должен молча менять цифры между двумя запусками.

Деградация graceful (курсы — ЧУЖИЕ данные): нет файла, битый JSON, нет нужной валюты ->
`None` и предупреждение в stdout. Вакансия выпадает из зарплатного среза, но прогон идёт.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings

# Алиасы валют -> канонический код курса. Дословная копия
# `hrwork/infrastructure/net/rates.py::CURRENCY_ALIAS` (RUR у HH = RUB, BYR = старый
# белорусский рубль, USDT = стейблкоин доллара). Копия, а не импорт: `etl/` монтируется
# в контейнер Airflow БЕЗ пакета `hrwork` (docker-compose: `./etl:/opt/airflow/etl`,
# `../data:ro`), и импорт родителя уронил бы DAG на старте. От расхождения защищает
# страж-тест `tests/test_domain.py::test_currency_alias_matches_parent`.
CURRENCY_ALIAS = {"RUR": "RUB", "BYR": "BYN", "USDT": "USD"}

_cache: dict[str, float] | None = None


def _warn(msg: str) -> None:
    # Логов в файлы проект не пишет: stdout забирает Promtail (см. CLAUDE.md).
    # Свой print, а не `pipeline.log`: pipeline импортирует domain, domain — этот модуль.
    print(f"[etl] FX: {msg}", flush=True)


def resolve_currency(code: str | None) -> str:
    """Код валюты -> канонический (RUR->RUB, BYR->BYN, USDT->USD). Пусто/None -> ''."""
    c = (code or "").upper()
    return CURRENCY_ALIAS.get(c, c)


def _read(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _warn(f"нет кеша курсов {path} — зарплаты в рублях не посчитаны")
        return {}
    except (OSError, ValueError) as e:
        _warn(f"кеш курсов не прочитан ({e}) — зарплаты в рублях не посчитаны")
        return {}
    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict) or not rates:
        _warn(f"в кеше курсов {path} нет ключа 'rates' — зарплаты в рублях не посчитаны")
        return {}
    return {str(k).upper(): float(v) for k, v in rates.items() if isinstance(v, (int, float))}


def load_rates(path: Path | None = None) -> dict[str, float]:
    """Курсы per-USD из кеша родителя. Нет файла/битый JSON -> `{}` (+ предупреждение).

    Читается ОДИН РАЗ на процесс: если бы файл перечитывался, обновление кеша посреди
    прогона дало бы разным вакансиям разные курсы, и три хранилища разошлись бы в цифрах
    на одном входе. `reset_cache()` — для тестов."""
    global _cache
    if _cache is None:
        _cache = _read(path or Settings.from_env().fx_file)
    return _cache


def reset_cache() -> None:
    """Сбросить прочитанные курсы (тесты; в проде процесс живёт один прогон)."""
    global _cache
    _cache = None


def to_rub(amount: float | None, currency: str | None,
           rates: dict[str, float] | None = None) -> float | None:
    """Сумма в валюте -> рубли по курсам per-USD. Нет суммы/валюты/курса -> None.

    ПУСТАЯ ВАЛЮТА — ТОЖЕ None, а не рубли. Родитель отдельно чинил это допущение
    08.08.2026: «HH по умолчанию рублёвый» верно для hh и катастрофично для восьми
    глобальных порталов (вилка $10 000 весила 10 000 ₽ и пряталась любым фильтром).
    Нет единицы измерения -> нет сравнимого числа: вакансия ВЫПАДАЕТ из зарплатного
    среза, а не считается нулём — ровно так поступает `analyzer.py` родителя."""
    if amount is None:
        return None
    r = rates if rates is not None else load_rates()
    src, rub = r.get(resolve_currency(currency)), r.get("RUB")
    if not src or not rub:
        return None
    return float(round(amount / src * rub))
