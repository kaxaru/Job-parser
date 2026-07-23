"""Единая точка входа CLI: python -m etl [--target …] [STEP …]."""
from __future__ import annotations

import argparse

from .pipeline import TARGETS, build_pipeline

STEPS = ("all", "init", "load")


def _step(value: str) -> str:
    # валидируем через type, а не choices: choices+nargs="*" в argparse ломает
    # пустой ввод (валидирует сам [] против choices, bpo-9625)
    if value not in STEPS:
        raise argparse.ArgumentTypeError(f"неизвестный шаг '{value}' (из: {', '.join(STEPS)})")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m etl",
        description="HH DWH ETL: extract+transform один раз, load в выбранные хранилища.",
        epilog="Примеры: '%(prog)s all' | '%(prog)s -t postgres load' | '%(prog)s -t clickhouse init'",
    )
    # action="append" (а не nargs="+"): иначе опция жадно съедает позиционный STEP,
    # и `-t postgres load` ломается. Повтор флага для нескольких: -t postgres -t clickhouse
    p.add_argument(
        "-t", "--target", action="append", choices=TARGETS, metavar="BACKEND",
        help=f"куда грузить (повторяемо): {', '.join(TARGETS)} (по умолчанию все)",
    )
    # пустой ввод -> []; Pipeline.run трактует пусто/`all` как весь конвейер
    p.add_argument(
        "steps", nargs="*", type=_step, metavar="STEP",
        help="шаги: all (по умолчанию) | init | load",
    )
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    targets = args.target or list(TARGETS)      # без -t -> все бэкенды
    build_pipeline(targets).run(args.steps)


if __name__ == "__main__":
    main()
