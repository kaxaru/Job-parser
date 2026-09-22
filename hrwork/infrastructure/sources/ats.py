"""Общая механика ATS-источников: обход РЕЕСТРА бордов работодателей.

Greenhouse и Ashby устроены одинаково и не похожи на остальные порталы: у них нет единой
выдачи, есть борд каждой компании по своему слагу. Значит нет ни пагинации, ни `total`,
ни «конца выдачи» — обход задаёт наш список имён, а не портал.

Отсюда две вещи, которых нет у постраничных адаптеров и которые живут ЗДЕСЬ, а не в копии
на каждый ATS (дубль класса между адаптерами — тот же дефект, что сведённый в base.py
`ListIncomplete`):

1. **Молчащий борд — норма, но не бесплатно.** Слаг, отдавший 404, значит «компания не на
   этой платформе или зовётся иначе», и ронять из-за него сбор нельзя. Но реестр протухает
   молча: компания переезжает с Greenhouse на Ashby, слаг умирает, и охват усыхает без
   единого сообщения. Поэтому доля молчащих считается и сравнивается с порогом.
2. **Полностью пустой ответ = сбой, а не пустой реестр.** Если не ответил НИ ОДИН борд, это
   сеть или бан, и наверх уходит `[]` — санити-гейт (`hh.py::_degraded_source`) на нулевом
   срезе не перезаписывает кеш.
"""
import asyncio
from collections.abc import Callable
from typing import Any

from hrwork.config import (
    ATS_BOARD_CONCURRENCY,
    ATS_DEAD_BOARDS_WARN,
    ATS_MAX_TIME,
    log,
)
from hrwork.infrastructure.net.http import RETRY_STANDARD, fetch_json_retry

from .hh import BROWSER_UA

#: Политика ретраев транспорта — единственный источник значения (net/http.py). Бэкофф 1 -> 8 с:
#: прогон длинный, а молчащий борд ничего дальше не отменяет (см. fetch_board).
RETRY = RETRY_STANDARD


def board_jobs(payload: Any) -> list[dict[str, Any]]:
    """Ответ борда ATS -> список карточек: и у Greenhouse, и у Ashby они лежат в `jobs`.

    Общая форма ответа — единственное, что у этих двух платформ совпадает дословно, поэтому
    извлекатель живёт ЗДЕСЬ, рядом с обходом реестра бордов, а не копией в каждом адаптере
    (аудит 22.09.2026, §3.1). Схемы самих карточек, наоборот, разные — они и остаются в
    адаптерах.
    """
    return list((payload or {}).get("jobs") or [])


async def fetch_board(url: str, extract: Callable[[Any], list[dict[str, Any]]],
                      *, source: str, board: str) -> list[dict[str, Any]] | None:
    """Один борд -> список карточек, либо None если борд не ответил.

    Разница между `[]` и `None` существенна и поэтому в типе: пустой список — борд живой,
    но вакансий сейчас нет (компания заморозила найм); None — борда нет или он не отдался.
    Свести их в `[]` значило бы потерять счётчик протухания реестра.

    Ретраятся только сбои ТРАНСПОРТА. Перепроверки пустого ответа, как у himalayas, здесь
    нет намеренно: там пустая страница неотличима от конца выдачи и обход по ней ОБРЫВАЛСЯ,
    а тут конца выдачи не существует — пустой борд ничего дальше не отменяет.

    Битый JSON при этом ретраится наравне со сбоем транспорта (общее правило `fetch_json_retry`),
    а вот ответ НЕ НАШЕЙ СХЕМЫ — нет: `extract` на нём бросает, и исключение уходит наружу
    («ретрай не поможет»). Раньше битый JSON тоже возвращал None сразу, и борд с обрезанным
    телом помечался МЁРТВЫМ, завышая долю протухшего реестра."""
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    try:
        return await fetch_json_retry(url, headers=headers, policy=RETRY, parse=extract,
                                      max_time=ATS_MAX_TIME,
                                      log_context=f"{source} board={board}")
    except (AttributeError, TypeError) as e:
        log.debug("{} board={}: {}", source, board, e)
        return None            # ответ пришёл, но это не наша схема — ретрай не поможет


async def collect_boards(boards: list[str], url_for: Callable[[str], str],
                         extract: Callable[[Any], list[dict[str, Any]]],
                         *, source: str) -> dict[str, list[dict[str, Any]]]:
    """Реестр слагов -> {слаг: карточки} только по ОТВЕТИВШИМ бордам.

    Молчащие считаются и попадают в лог: ниже порога `ATS_DEAD_BOARDS_WARN` — INFO,
    выше — WARNING с перечнем. Пустой результат наверх уходит как `{}`.
    """
    sem = asyncio.Semaphore(ATS_BOARD_CONCURRENCY)

    async def _one(slug: str) -> tuple[str, list[dict[str, Any]] | None]:
        async with sem:
            return slug, await fetch_board(url_for(slug), extract, source=source, board=slug)

    pairs = await asyncio.gather(*(_one(b) for b in boards))
    alive = {slug: jobs for slug, jobs in pairs if jobs is not None}
    dead = [slug for slug, jobs in pairs if jobs is None]

    if dead:
        share = len(dead) / len(boards) if boards else 1.0
        head = ", ".join(sorted(dead)[:10])
        if share >= ATS_DEAD_BOARDS_WARN:
            log.warning("{}: НЕ ОТВЕТИЛИ {} бордов из {} ({:.0%}) — охват усох. Причина одна "
                        "из трёх: реестр протух, источник заблокирован, ответы не уложились "
                        "в ATS_MAX_TIME={} с. Слаги: {}",
                        source, len(dead), len(boards), share, ATS_MAX_TIME, head)
        else:
            # Причину НЕ называем. `fetch_bytes` схлопывает 404 и сбой транспорта в один
            # `None`, поэтому «компания не на этой платформе» было бы догадкой, выданной
            # за факт. 14.08.2026 именно эта формулировка спрятала таймаут на шести самых
            # тяжёлых бордах: в логе стояло «сменила слаг», а на деле борды были живы.
            log.info("{}: бордов без ответа {} из {} (нет борда по слагу либо ответ не "
                     "уложился в ATS_MAX_TIME={} с): {}",
                     source, len(dead), len(boards), ATS_MAX_TIME, head)
    if not alive:
        log.warning("{}: не ответил НИ ОДИН борд из {} — источник недоступен",
                    source, len(boards))
    return alive


def pretty_company(slug: str) -> str:
    """Слаг борда -> подпись работодателя, когда своего имени в схеме нет.

    Тот же приём и та же оговорка, что в `himalayas.py::_employer`: точное имя это не
    восстанавливает (регистр аббревиатур и знаки препинания теряются), но для подписи в
    ленте и для ключа кросс-портального дедупа (пара работодатель+тайтл) стабильно
    и различимо. Применять ТОЛЬКО когда портал имени не отдаёт: у greenhouse оно есть
    полем `company_name`, и брать вместо него слаг было бы порчей данных.
    """
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)
