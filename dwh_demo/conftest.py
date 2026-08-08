import sys
from pathlib import Path

import pytest

# делает пакет etl импортируемым при запуске pytest из dwh_demo
sys.path.insert(0, str(Path(__file__).resolve().parent))


#: Синтетические курсы per-USD для юнит-прогона. Числа круглые и НЕ равны рыночным
#: намеренно: если тест зелен только на живом курсе, он проверяет курс, а не код.
FX_FOR_TESTS = {"USD": 1.0, "RUB": 100.0, "EUR": 0.5, "BYN": 3.0}


@pytest.fixture(autouse=True)
def _fx_rates_are_synthetic(monkeypatch, request):
    """Курсы в юнит-прогоне — СВОИ, а не суточный кеш родителя (`data/fx_rates.json`).

    Опт-аут маркером `real_fx` — он нужен ровно тем тестам, чей ПРЕДМЕТ и есть сам
    загрузчик курсов: подменять то, что проверяешь, бессмысленно.

    Инцидент 09.08.2026: конвейер начал конвертировать вилки в рубли и по умолчанию читал
    кеш родителя. Юнит-тест сразу же стал зависеть от того, лежит ли файл на диске и какой
    в нём курс, — ровно тот класс дефекта, который аудит того же дня разбирал у родителя
    («тест зелёный, потому что читает чужое состояние машины»). Прогон на чистом клоне
    и на машине владельца обязан давать один результат.
    """
    if request.node.get_closest_marker("real_fx"):
        return
    from etl import rates
    monkeypatch.setattr(rates, "load_rates", lambda path=None: dict(FX_FOR_TESTS))
    monkeypatch.setattr("etl.pipeline.load_rates", lambda path=None: dict(FX_FOR_TESTS))


def pytest_collection_modifyitems(items):
    """Всё, что не помечено `integration`, автоматически считается `unit`.

    Локальный прогон отбирает `-m "not integration"` (pytest.ini), CI — `-m unit`
    (.github/workflows/ci.yml). Пока маркер проставляется вручную, эти два отбора
    расходятся молча: файл, где забыли `pytestmark = pytest.mark.unit`, локально идёт,
    а в CI не выполняется вовсе. `--strict-markers` ловит ОПЕЧАТКУ в имени маркера,
    но не его ОТСУТСТВИЕ — поэтому маркер навешивается здесь, а не проверяется.
    """
    for item in items:
        if "integration" not in item.keywords:
            item.add_marker(pytest.mark.unit)
