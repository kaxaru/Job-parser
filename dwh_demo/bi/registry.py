"""Реестр дашбордов: ключ CLI -> класс. Добавить дашборд = добавить сюда строку.

Тип `dict[str, type[Dashboard]]` — статическая проверка (mypy): класс обязан
структурно соответствовать Protocol Dashboard. Рантайм-проверку даёт тест.
"""
from .dashboards.base import Dashboard
from .dashboards.comparison import ComparisonDashboard
from .dashboards.cooccurrence import CooccurrenceDashboard
from .dashboards.mssql_overview import MssqlOverviewDashboard
from .dashboards.overview import OverviewDashboard
from .dashboards.source_comparison import SourceComparisonDashboard

REGISTRY: dict[str, type[Dashboard]] = {
    cls.key: cls for cls in (
        OverviewDashboard, ComparisonDashboard, CooccurrenceDashboard, MssqlOverviewDashboard,
        SourceComparisonDashboard,
    )
}
