"""Источники вакансий (порталы-скраперы) + реестр Source.

Публичный API — из base; импорт конкретных источников ниже регистрирует их фабрики
(side-effect). Новый портал = модуль здесь + строка импорта.
"""
# Регистрация источников (side-effect импорта). HHSource регистрируется в base при импорте выше;
# hirify — здесь. Новый портал -> добавить строку.
from . import arbeitnow as _arbeitnow  # noqa: F401
from . import getmatch as _getmatch  # noqa: F401
from . import himalayas as _himalayas  # noqa: F401
from . import hirify as _hirify  # noqa: F401
from . import jobicy as _jobicy  # noqa: F401
from . import talanto as _talanto  # noqa: F401
from . import themuse as _themuse  # noqa: F401
from . import web3career as _web3career  # noqa: F401
from .base import Source, get_source, register_source

__all__ = ["Source", "get_source", "register_source"]
