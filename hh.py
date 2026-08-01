#!/usr/bin/env python3
"""
HH.ru Job Market Analyzer — рынок труда для программистов.

Режимы запуска:
  python hh.py collect [--force]  — собрать вакансии (data/vacancies_raw.json)
  python hh.py analyze            — отчёты по сохранённым данным (консоль + CSV)
  python hh.py dashboard          — интерактивный HTML-дашборд (data/dashboard.html)
  python hh.py feed               — HTML-лента вакансий (data/feed.html)
  python hh.py serve [--port N]   — локальный сервер ленты + автосейв отметок
  python hh.py autoclick [--login] [--bump-only|--apply-only]
                         [--apply-limit N] [--daily-cap N] [--headed]
                                  — Playwright: поднять резюме + автоотклики
  python hh.py chat [--send] [--reply-limit N]
                                  — шаблонные ответы бот-рекрутерам (без --send: dry-run)
  python hh.py [all] [--force]    — collect + analyze
"""
import argparse
import asyncio
from collections import Counter
from enum import Enum
from typing import Any

from hrwork.config import (
    COLLECT_MIN_RATIO,
    COLLECT_SANITY_MIN,
    HH_DAILY_APPLY_CAP,
    RAW_FILE,
    SERVE_PORT,
    SOURCES,
    log,
)
from hrwork.infrastructure import storage
from hrwork.infrastructure.net.proxy import load_proxies, mask_proxy
from hrwork.infrastructure.sources import get_source
from hrwork.infrastructure.sources.hh import HHHtmlClient
from hrwork.infrastructure.storage import JsonVacancyRepository
from hrwork.presentation.views.reporter import run_reports


def _degraded_source(by_src: Counter[str], prior_by_src: Counter[str]) -> str | None:
    """Первый источник, просевший ниже COLLECT_MIN_RATIO от прошлого среза (при заметном
    прошлом объёме >= COLLECT_SANITY_MIN) — признак блока/сбоя. Иначе None."""
    for src, prev_n in prior_by_src.items():
        if prev_n >= COLLECT_SANITY_MIN and by_src.get(src, 0) < prev_n * COLLECT_MIN_RATIO:
            return src
    return None


async def collect(force: bool = False) -> list[Any]:
    repo = JsonVacancyRepository()
    if storage.cache_valid(force):
        return repo.load()

    proxy_rotator = load_proxies()
    proxies: list[str] = []
    if proxy_rotator.enabled:
        proxies = proxy_rotator.next_batch(proxy_rotator.size)
        log.info('Прокси загружены: {} (ротация по кругу, пример: {})',
                 proxy_rotator.size, mask_proxy(proxies[0]))
    else:
        log.info('Прокси не используются (файл отсутствует или HH_DISABLE_PROXIES=1), используем прямое подключение.')

    # Агрегатор: собираем все включённые порталы ПАРАЛЛЕЛЬНО через реестр Source.
    # collect() не знает про конкретные классы — только про имена из config.SOURCES.
    # Каждый источник отдаёт list[VacancyRecord] (ACL внутри адаптера).
    async def _run_source(name: str) -> list[Any]:
        src = get_source(name, proxies=proxies)
        if src is None:
            log.warning('Неизвестный источник: {} (нет в реестре) — пропуск', name)
            return []
        try:
            got = await src.collect()
        except Exception as e:
            log.error('Источник {} упал: {} — пропуск', name, e)
            return []
        log.info('Источник {}: {} вакансий', name, len(got))
        return got

    results = await asyncio.gather(*(_run_source(s) for s in SOURCES))
    seen: set[str] = set()      # merge + дедуп по id (id неймспейснуты по источнику)
    items = []
    for lst in results:
        for rec in lst:
            if rec.vacancy.id in seen:
                continue
            seen.add(rec.vacancy.id)
            items.append(rec)

    if not items:
        log.error('Сбор не дал ни одной вакансии (блокировка/сеть?) — кеш не перезаписываем.')
        return repo.load() if repo.exists() else []
    by_src = Counter(r.vacancy.source for r in items)
    # Санити-гейт: транзиентный блок источника на этапе 1 даёт резко меньший срез. Не затираем
    # полный кеш деградированным (иначе теряем историю + ужимаем desc-кеш). --force обходит.
    if not force and repo.exists():
        prior_by_src = Counter(r.vacancy.source for r in repo.load())
        bad = _degraded_source(by_src, prior_by_src)
        if bad is not None:
            log.error('Источник {}: {} << {} (< {:.0%}) — вероятен блок/сбой, кеш НЕ '
                      'перезаписываю. Повтор при реальном спаде: --force.',
                      bad, by_src.get(bad, 0), prior_by_src[bad], COLLECT_MIN_RATIO)
            return repo.load()
    log.success('Всего уникальных: {} вакансий  ({})', len(items),
                ', '.join(f'{k}: {v}' for k, v in by_src.items()))
    repo.save(items)
    log.info('Сырые данные: {}', RAW_FILE)
    return items


async def enrich(only_empty: bool = False) -> None:
    """Дозагрузить карточки (описание + HTML) поверх собранного vacancies_raw.json.

    only_empty=True — добирать лишь вакансии без описания; иначе обновить все
    (нужно, чтобы получить HTML-форматирование для уже собранных).
    """
    repo = JsonVacancyRepository()
    records = repo.load()
    if not records:
        log.warning('Кеш пуст — дозагружать нечего. Сначала: python hh.py collect --force')
        return
    log.info('Дозагрузка карточек: {} вакансий в кеше', len(records))

    proxy_rotator = load_proxies()
    proxies = proxy_rotator.next_batch(proxy_rotator.size) if proxy_rotator.enabled else []
    if proxies:
        log.info('Прокси: {} (ротация)', proxy_rotator.size)

    client = HHHtmlClient(proxies=proxies)
    await client.run_enrich(records, skip_filled=only_empty)

    repo.save(records)
    have = sum(1 for r in records if r.description_html)
    log.success('Описаний: {} из {} ({}%). Сохранено: {}',
                have, len(records), have * 100 // len(records), RAW_FILE)


def analyze(records: list[Any] | None = None) -> None:
    if records is None:
        log.info('Загрузка {}...', RAW_FILE)
        records = JsonVacancyRepository().load()

    log.info('Обработка {} вакансий...', len(records))
    vacs = [r.vacancy for r in records]
    if not vacs:
        log.warning('Нет вакансий для анализа. Сначала: python hh.py collect --force')
        return
    with_sal = sum(1 for v in vacs if v.salary)
    log.info('С зарплатой: {} ({}%)', with_sal, with_sal * 100 // len(vacs))
    run_reports(vacs)


class Mode(Enum):
    """Режим запуска CLI вместо «голых строк»: argparse парсит ввод сразу в Mode
    (type=Mode), а диспетчеризация — по таблице _HANDLERS, без цепочки if/elif."""
    ALL = 'all'
    COLLECT = 'collect'
    ANALYZE = 'analyze'
    ENRICH = 'enrich'
    DASHBOARD = 'dashboard'
    FEED = 'feed'
    SERVE = 'serve'
    AUTOCLICK = 'autoclick'
    CHAT = 'chat'
    FORMS = 'forms'
    HHAPI = 'hhapi'

    def __str__(self) -> str:        # argparse печатает значение (all/collect/…), а не «Mode.ALL»
        return self.value

    def run(self, args: argparse.Namespace) -> None:
        _HANDLERS[self](args)


def _do_collect(args: argparse.Namespace) -> None:
    asyncio.run(collect(force=args.force))


def _do_enrich(args: argparse.Namespace) -> None:
    asyncio.run(enrich(only_empty=args.only_empty))


def _do_analyze(args: argparse.Namespace) -> None:
    analyze()


def _do_dashboard(args: argparse.Namespace) -> None:
    # лениво: только этот режим тянет тяжёлый рендер дашборда
    from hrwork.presentation.views.dashboard import build_dashboard
    build_dashboard()


def _do_feed(args: argparse.Namespace) -> None:
    from hrwork.presentation.views.feed import build_feed
    build_feed()


def _do_serve(args: argparse.Namespace) -> None:
    from hrwork.presentation.server import run_server
    run_server(port=args.port)


def _do_autoclick(args: argparse.Namespace) -> None:
    # лениво: playwright — опциональная зависимость, нужен только этому режиму
    from hrwork.application.apply import autoclick
    if args.login:
        autoclick.login()
    elif args.sync_status or args.sync_full:
        autoclick.sync_statuses(headless=not args.headed, full=args.sync_full)
    else:
        autoclick.run(apply_limit=args.apply_limit, daily_cap=args.daily_cap,
                      headless=not args.headed, cover_mode=args.cover,
                      do_bump=not args.apply_only, do_apply=not args.bump_only,
                      force_bump=args.bump_only)   # явный --bump-only обходит кулдаун-гейт


def _do_chat(args: argparse.Namespace) -> None:
    # шаблонные автоответы бот-рекрутерам; браузер не нужен (cookie-only chatik API)
    from hrwork.application.apply.chat import chat_reply
    # poll ответов бота идёт автоматически внутри run() после отправки (wait=True);
    # параметр wait оставлен для тестов и программного вызова, ручкой CLI не выведен
    chat_reply.run(send=args.send, limit=args.reply_limit, only=args.only,
                   include_manual=args.include_manual,
                   max_rounds=args.loop_rounds if args.loop else 1,
                   use_intent=args.intent, use_rephrase=args.rephrase)


def _do_forms(args: argparse.Namespace) -> None:
    # форм-очередь: тот же авто-путь, что inline apply (заполнить + «Откликнуться» при полноте,
    # пробелы -> лог). Браузерный путь (берёт autoclick.lock); Playwright опционален. --dry = превью.
    # --sweep — снять структуру всех анкет в кеш (forms_cache.json), пропуская уже собранные.
    from hrwork.application.apply.forms import forms
    if args.clean:                                 # вычистить очередь (мёртвые/уже-откликнутые), без браузера
        forms.clean_queue()
        return
    if args.sweep:
        forms.sweep(only=args.only, headless=not args.headed, refresh=args.refresh)
        return
    forms.run(dry=args.dry, only=args.only, headless=not args.headed,
              cover_mode=args.cover, limit=args.limit)


def _do_hhapi(args: argparse.Namespace) -> None:
    # официальный API как ВТОРОЙ путь (browser остаётся): --login один раз, --probe смотрит права
    from hrwork.infrastructure.sources import hh_api
    if args.login:
        hh_api.login()
        return
    hh_api.probe()


def _do_all(args: argparse.Namespace) -> None:
    asyncio.run(collect(force=args.force))    # collect сохраняет файл
    analyze()                                 # -> repo.load() читает свежесохранённое


_HANDLERS = {
    Mode.COLLECT:   _do_collect,
    Mode.ENRICH:    _do_enrich,
    Mode.ANALYZE:   _do_analyze,
    Mode.DASHBOARD: _do_dashboard,
    Mode.FEED:      _do_feed,
    Mode.SERVE:     _do_serve,
    Mode.AUTOCLICK: _do_autoclick,
    Mode.CHAT:      _do_chat,
    Mode.FORMS:     _do_forms,
    Mode.HHAPI:     _do_hhapi,
    Mode.ALL:       _do_all,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', nargs='?', default=Mode.ALL, type=Mode, choices=list(Mode),
                        help='режим запуска (по умолчанию all = collect + analyze)')
    parser.add_argument('--force', action='store_true', help='игнорировать кеш и пересобрать данные')
    parser.add_argument('--only-empty', action='store_true',
                        help='enrich: добирать только вакансии без описания')
    parser.add_argument('--port', type=int, default=SERVE_PORT, help='serve: порт локального сервера')
    parser.add_argument('--login', action='store_true',
                        help='autoclick: разовый вход с окном браузера (сессия сохраняется); '
                             'hhapi: разовая OAuth-авторизация своего приложения (dev.hh.ru)')
    parser.add_argument('--apply-limit', type=int, default=None,
                        help='autoclick: максимум откликов за ЗАПУСК (по умолчанию 10)')
    parser.add_argument('--daily-cap', type=int, default=HH_DAILY_APPLY_CAP,
                        help='autoclick: потолок откликов в СУТКИ (лимит HH ~200)')
    parser.add_argument('--cover', choices=['template', 'llm'], default='template',
                        help='autoclick: сопроводительное письмо — шаблон (по умолч.) или LLM')
    parser.add_argument('--sync-status', action='store_true',
                        help='autoclick: собрать статусы откликов (отказ/приглашение/…) -> response_status.json; '
                             'инкремент — чаты без активности 7 дней берутся из кеша')
    parser.add_argument('--sync-full', action='store_true',
                        help='autoclick: полный синк ВСЕХ чатов без кеша (долго, ~50 чатов/45с)')
    parser.add_argument('--headed', action='store_true',
                        help='autoclick: показывать окно браузера (отладка)')
    # chat: по умолчанию DRY-RUN — реальная отправка только с явным --send
    parser.add_argument('--send', action='store_true',
                        help='chat: РЕАЛЬНО отправить ответы (без флага — только показать)')
    parser.add_argument('--reply-limit', type=int, default=10,
                        help='chat: максимум ответов за запуск (по умолчанию 10)')
    parser.add_argument('--only', default='',
                        help='chat: ответить ТОЛЬКО по этой вакансии (id) — для точечной проверки')
    parser.add_argument('--include-manual', action='store_true',
                        help='chat: слать и ответы про деньги/место — КАЖДЫЙ с подтверждением '
                             '[y/N] в терминале (без tty пропускаются)')
    parser.add_argument('--loop', action='store_true',
                        help='chat: вести диалог до конца — после ответа ждать следующий вопрос '
                             'бота и отвечать на него; тупик (нет факта/деньги/место) -> человеку')
    parser.add_argument('--loop-rounds', type=int, default=8,
                        help='chat: предохранитель от зацикливания --loop (по умолчанию 8)')
    parser.add_argument('--intent', action='store_true',
                        help='chat: маршрутизировать вопросы через LLM-классификатор намерения '
                             '(нужен INTENT_LLM=1 в .env; без него — regex как раньше)')
    parser.add_argument('--rephrase', action='store_true',
                        help='chat: переформулировать одобренный ответ под вопрос через LLM '
                             '(нужен REPHRASE_LLM=1; изменённый текст — под [y/N] в терминале, '
                             'крон/без tty шлёт источник дословно; факты не добавляются)')
    parser.add_argument('--dry', action='store_true',
                        help='forms: только показать поля/резолвинг форм-очереди, ничего не жать. '
                             'Без флага (нужен FORMS_LLM=1) — авто: полные анкеты заполняются и '
                             'отправляются «Откликнуться», пробелы -> лог (пополни form_answers)')
    parser.add_argument('--sweep', action='store_true',
                        help='forms: снять структуру ВСЕХ анкет очереди в кеш (forms_cache.json), '
                             'пропуская уже собранные; submit не жмётся. Разовый каталог вопросов/опций')
    parser.add_argument('--refresh', action='store_true',
                        help='forms --sweep: пересобрать даже уже закешированные формы (игнор кеша)')
    parser.add_argument('--clean', action='store_true',
                        help='forms: убрать из очереди мёртвые (истёкшие/без формы) и уже-откликнутые '
                             '(в архив — вакансии остаются в feed); без браузера')
    parser.add_argument('--limit', type=int, default=0,
                        help='forms: максимум ОТПРАВЛЕННЫХ откликов за запуск (дренаж бэклога батчами)')
    _mode_grp = parser.add_mutually_exclusive_group()
    _mode_grp.add_argument('--bump-only', action='store_true',
                           help='autoclick: только поднять резюме (задача раз в 4ч)')
    _mode_grp.add_argument('--apply-only', action='store_true',
                           help='autoclick: только отклики без поднятия (дневная задача)')
    args = parser.parse_args()

    args.mode.run(args)


if __name__ == '__main__':
    main()
