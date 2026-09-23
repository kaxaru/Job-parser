"""Уборка logs/: ротация логов кронов по размеру и чистка per-PID логов loguru по возрасту.

Зачем (24.09.2026): каталог дорос до 1,2 ГБ. Логи кронов пишет cmd (`>> logs\\cron_*.log` в
`cron/*.bat`), и ротации у них не было вовсе — `cron_collect.log` набрал 480 МБ, в основном
печатными таблицами отчётов (~7 МБ за сбор). Per-PID логи loguru (`config.py`,
`hr_work_<pid>.log`) ротируются самим loguru, но `retention=3` действует в пределах ОДНОГО
PID, а PID у каждого запуска свой: 2665 файлов, 701 МБ с 20.07, и никто их не удалял.

Запуск — первым шагом `cron/cron_collect.bat` (раз в сутки):
    python -m hrwork.infrastructure.storage.logfiles
Вывод этого шага в `cron_collect.log` НЕ перенаправляется: cmd открывает файл редиректа ДО
старта процесса, и переименовать лог, который держит собственный же редирект, Windows не даст.
Итог пишется в per-PID лог loguru.

Политика восстановления: каждый файл — независимая операция. Файл, который держит другой
процесс (WinError 32: в 12:00 рядом стартует hh_chat и пишет свой лог), пропускается молча и
уберётся в следующие сутки; уборка не роняет крон. Повтор безопасен: ротированный `*.1` не
ротируется повторно (шаблон `cron_*.log`), а удалённое не удаляется дважды.
"""
import datetime
import os
from pathlib import Path
from typing import NamedTuple

MAX_CRON_LOG_BYTES = 20 * 1024 * 1024                  # живой лог + одно поколение `.1` = ~40 МБ
# Две недели: per-PID лог — персональный сток (фрагменты переписки, `config.body`), держать его
# месяцами незачем, а разбор инцидента по логам идёт в пределах дней.
PROCESS_LOG_MAX_AGE = datetime.timedelta(days=14)
_CRON_GLOB = "cron_*.log"            # `cron_x.log.1` сюда не попадает — уже ротирован
_PROCESS_GLOB = "hr_work_*.log"      # и живые `hr_work_<pid>.log`, и ротированные loguru `hr_work_<pid>.<ts>.log`


class Housekeeping(NamedTuple):
    """Итог прохода: какие логи кронов ротированы и сколько per-PID логов удалено."""
    rotated: tuple[str, ...]
    pruned: int


def rotate_by_size(path: Path, max_bytes: int) -> bool:
    """Больше `max_bytes` -> `<имя>.1` (прежний `.1` заменяется: храним одно поколение).
    False — не превышен, файла нет или его держит другой процесс (пропуск до следующего раза).
    Через `os.replace`, а не `Path.replace`: так подмена в тестах видит ровно этот вызов."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:              # нет файла / WinError 32 — не наша ошибка, крон не роняем
        return False
    return True


def prune_process_logs(folder: Path, max_age: datetime.timedelta,
                       now: datetime.datetime | None = None) -> int:
    """Удалить per-PID логи loguru старше `max_age` (по mtime). Возвращает, сколько удалено;
    открытый живым процессом файл пропускается."""
    moment = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    edge = (moment - max_age).timestamp()
    deleted = 0
    for p in sorted(folder.glob(_PROCESS_GLOB)):
        try:
            if p.stat().st_mtime >= edge:
                continue
            os.unlink(p)
        except OSError:
            continue
        deleted += 1
    return deleted


def housekeep(folder: Path, *, max_bytes: int = MAX_CRON_LOG_BYTES,
              max_age: datetime.timedelta = PROCESS_LOG_MAX_AGE,
              now: datetime.datetime | None = None) -> Housekeeping:
    """Один проход по каталогу логов: ротация логов кронов + чистка per-PID логов."""
    rotated = tuple(p.name for p in sorted(folder.glob(_CRON_GLOB)) if rotate_by_size(p, max_bytes))
    return Housekeeping(rotated=rotated, pruned=prune_process_logs(folder, max_age, now))


def main() -> None:
    from hrwork.config import LOGS_DIR, log
    res = housekeep(LOGS_DIR)
    log.info("Уборка logs/: ротировано логов кронов {} ({}), удалено per-PID логов старше {} дн: {}",
             len(res.rotated), ", ".join(res.rotated) or "—", PROCESS_LOG_MAX_AGE.days, res.pruned)


if __name__ == "__main__":
    main()
