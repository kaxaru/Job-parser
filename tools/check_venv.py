"""Гейт окружения для pre-commit: коммит идёт из .venv3, а не из системного Python.

Хуки объявлены с `language: system`, то есть берут инструменты из АКТИВНОГО окружения.
Если venv не активирован, `python -m pytest` уходит в системный интерпретатор без
зависимостей проекта и падает грудой `ModuleNotFoundError: No module named 'dotenv'` —
37 ошибок сборки, в которых настоящая причина не видна вовсе (инцидент 07.08.2026).

Проверяем именно ВЕРСИЮ, а не путь: venv лежит по-разному (../.venv3 у автора, .venv3
внутри репо по README), а 3.10 — его floor, зафиксированный в ruff.toml (target-version).
Системный Python на машине 3.14, так что расхождение ловится однозначно.
"""
import sys

REQUIRED = (3, 10)


def main() -> int:
    if sys.version_info[:2] == REQUIRED:
        return 0
    got = ".".join(str(x) for x in sys.version_info[:3])
    want = ".".join(str(x) for x in REQUIRED)
    print(f"Не тот Python: {got}, нужен {want} из .venv3.")
    print("Активируй окружение и повтори — см. CLAUDE.md, раздел «Ловушки окружения»:")
    print(r"    ..\.venv3\Scripts\activate")
    print("Либо разово в обход проверок: git commit --no-verify")
    return 1


if __name__ == "__main__":
    sys.exit(main())
