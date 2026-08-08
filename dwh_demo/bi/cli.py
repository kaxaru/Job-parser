"""Единый вход BI-провижининга: python -m bi [NAME ... | all].

NAME — ПОЗИЦИОННЫЙ аргумент (ключ из REGISTRY); флага `--dashboard` у парсера нет
и никогда не было — докстринг обещал его до 09.08.2026 и давал SystemExit 2."""
from __future__ import annotations

import argparse

from .client import MetabaseClient
from .config import CLICKHOUSE, MSSQL, POSTGRES, Settings
from .registry import REGISTRY

_CHOICES = ("all", *REGISTRY)


def _dashboard(value: str) -> str:
    # type-валидатор вместо choices: choices+nargs="*" ломает пустой ввод (bpo-9625)
    if value not in _CHOICES:
        raise argparse.ArgumentTypeError(f"неизвестный дашборд '{value}' (из: {', '.join(_CHOICES)})")
    return value


def run(keys) -> None:
    cfg = Settings()
    client = MetabaseClient(cfg.base, cfg.admin_email, cfg.admin_password)
    client.connect()
    client.ensure_database(**POSTGRES)
    client.ensure_database(**CLICKHOUSE)
    client.ensure_database(**MSSQL)
    print("провижу дашборды:")
    for key in keys:
        REGISTRY[key]().build(client)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bi",
        description="Провижининг дашбордов Metabase из кода (идемпотентно).",
    )
    p.add_argument(
        # пустой ввод -> []; resolve() трактует пусто/`all` как все
        "dashboards", nargs="*", type=_dashboard, metavar="NAME",
        help=f"какие дашборды: all (по умолчанию) | {' | '.join(REGISTRY)}",
    )
    return p


def resolve(dashboards) -> list:
    """Пусто или 'all' -> все зарегистрированные дашборды; иначе выбранные."""
    if not dashboards or "all" in dashboards:
        return list(REGISTRY)
    return dashboards


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    run(resolve(args.dashboards))


if __name__ == "__main__":
    main()
