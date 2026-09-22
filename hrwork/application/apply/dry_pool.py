"""Пул кандидатов без браузера — `hh.py autoclick --dry-pool` (RFC-004).

Показывает, куда пойдут отклики, ДО того как отклик станет необратимым: размер пула, ярусы,
роли, топ работодателей и тайтлов, причины отсева и конкретные названия, отбитые блок-листом
работодателей — по ним видно, все ли написания пойманы. Ничего не пишет и браузер не поднимает,
поэтому безопасен рядом с идущим кроном (lock не берёт).
"""
from collections import Counter
from dataclasses import dataclass
from typing import Any

from hrwork.application.apply import ab_split, candidates
from hrwork.application.apply.ab_split import PoolInputs
from hrwork.application.apply.candidates import Candidate, employer_blocked, pick_candidates
from hrwork.application.apply.forms.form_status import skippable_form_ids
from hrwork.application.apply.runtime.store import store
from hrwork.config import ACCOUNT, log
from hrwork.infrastructure.storage import vacancy_repository

TOP_N = 10          # строк в топах работодателей и тайтлов


@dataclass(frozen=True)
class PoolReport:
    """Срез пула — всё, что нужно человеку, чтобы утвердить отбор до первого отклика."""
    pool: tuple[Candidate, ...]              # в порядке очереди отклика
    tiers: dict[str, int]                    # ярус -> вакансий, в порядке очереди
    roles: dict[str, int]                    # роль -> вакансий, по убыванию
    employers: list[tuple[str, int]]         # топ TOP_N
    titles: list[tuple[str, int]]            # топ TOP_N
    rejected: dict[str, int]                 # причина отсева -> вакансий, по убыванию
    blocked_employers: dict[str, int]        # название работодателя HH -> вакансий, по убыванию
    blocklist_active: bool                   # False — блок-лист в .env пуст


def _by_count(counter: Counter[str]) -> dict[str, int]:
    """По убыванию счёта, при равенстве — по имени: вывод стабилен между запусками."""
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def build_report(records: list[Any], inputs: PoolInputs,
                 form_ids: set[str] | frozenset[str] = frozenset()) -> PoolReport:
    """Весь пул (без лимита запуска) тем же `pick_candidates`, что и боевой батч."""
    stats: dict[str, int] = {}
    pool = pick_candidates(records, inputs, len(records), form_ids=form_ids, stats=stats)
    role_by_id = {r.vacancy.id: r.vacancy.role for r in records}
    tiers: Counter[str] = Counter(c.tier.name for c in pool if c.tier is not None)
    return PoolReport(
        pool=tuple(pool),
        tiers={t.name: tiers[t.name] for t in candidates.ApplyTier if tiers[t.name]},
        roles=_by_count(Counter(role_by_id[c.id].label for c in pool)),
        employers=list(_by_count(Counter(c.employer or "—" for c in pool)).items())[:TOP_N],
        titles=list(_by_count(Counter(c.name for c in pool)).items())[:TOP_N],
        rejected=_by_count(Counter(stats)),
        # По всему кешу HH, а не только по отсеву: отметка или анкета стоят в `_reject_reason`
        # раньше блок-листа, и отбитое ими название иначе не попало бы в список написаний.
        blocked_employers=_by_count(Counter(r.vacancy.employer for r in records
                                            if r.vacancy.source == "hh"
                                            and employer_blocked(r.vacancy.employer))),
        blocklist_active=candidates.APPLY_EMPLOYER_BLOCK is not None,
    )


def _pairs(items: dict[str, int] | list[tuple[str, int]]) -> str:
    return ", ".join(f"{k} {n}" for k, n in (items.items() if isinstance(items, dict) else items))


def format_report(report: PoolReport) -> list[str]:
    """Строки лога. ASCII-разделители: терминал cp1251."""
    if not report.blocklist_active:
        blocked = "пуст (APPLY_EMPLOYER_BLOCKLIST в .env не задан)"
    else:
        blocked = _pairs(report.blocked_employers) or "совпадений в кеше нет"
    return [
        f"Пул кандидатов (dry-pool) [{ACCOUNT.code}]: {len(report.pool)}",
        f"  ярусы: {_pairs(report.tiers) or '-'}",
        f"  роли: {_pairs(report.roles) or '-'}",
        f"  топ работодателей: {_pairs(report.employers) or '-'}",
        f"  топ тайтлов: {_pairs(report.titles) or '-'}",
        f"  отсев: {_pairs(report.rejected) or '-'}",
        f"  блок-лист работодателей: {blocked}",
    ]


def run(write_eligible: bool = False) -> PoolReport:
    """Загрузить кеш, marks и форм-очередь так же, как `autoclick._apply_batch`, и напечатать срез.
    `write_eligible` — второй аккаунт заодно освежает общую часть A/B (`ab_split.py`): после
    сбора, чтобы основной не простаивал на устаревшей до следующего прогона второго."""
    records = vacancy_repository().load()
    published = {r.id: r.vacancy.published_at for r in records}
    form_ids = skippable_form_ids(store.forms(), store.form_cache(), published)
    if write_eligible:
        ab_split.write_eligible(c.id for c in pick_candidates(records, ab_split.PoolInputs(),
                                                              len(records)))
    split = ab_split.current_split()
    # Те же входы пула, что и у боевого _apply_batch (ab_split.pool_inputs): превью не должно
    # расходиться с реальным прогоном. Для acc2 это его ВЕСЬ пул (дедуп против своей истории).
    report = build_report(records, ab_split.pool_inputs(split), form_ids)
    for line in format_report(report):
        log.info(line)
    return report
