"""Межпроцессная блокировка на lock-файле (RFC-004).

`threading.Lock` сериализует писателей только внутри процесса. У `marks.json` писателей
несколько ПРОЦЕССОВ — сервер ленты, крон откликов каждого аккаунта, синк, — и цикл
«прочитал -> изменил -> записал» без общей блокировки теряет чужую запись: потерянная
отметка «отказ» оборачивается необратимым откликом.

Блокировку держит ОС на открытом дескрипторе (`msvcrt.locking` / `fcntl.flock`): умерший
процесс её отпускает сам, протухших lock-файлов не бывает. Сам файл пустой и не удаляется.
"""
import contextlib
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO


class FileLockTimeout(TimeoutError):
    """Блокировку не дали за отведённое время — писатель обязан упасть, а не писать без неё."""


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)   # OSError, если занято

    def _unlock(fh: IO[bytes]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # OSError, если занято

    def _unlock(fh: IO[bytes]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float, poll: float = 0.02) -> Iterator[None]:
    """Эксклюзивная блокировка `path` на время блока; `FileLockTimeout` по истечении `timeout`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(fh)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(
                        f"{path.name}: блокировку не дали за {timeout:.0f} с") from None
                time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fh)
