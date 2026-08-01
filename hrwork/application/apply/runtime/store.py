"""Фасад состояния откликов — единая точка над разрозненными файлами.

Состояние «отклика» физически размазано по шести файлам (marks / apply_quota /
applied_log / response_status / form_vacancies / apply_pending) и трём модулям
(storage.marks, apply.quota, storage.followup). ApplicationStore собирает их за одним
объектом с доменным словарём («отмечен», «квота», «журнал», «статусы», «форма», «очередь»),
чтобы autoclick/server/feed не тянулись в шесть мест.

Это ЛЁГКИЙ фасад: логику НЕ дублирует и НЕ добавляет — делегирует существующим (тестируемым)
функциям. Полноценная сущность Application / доменные события — отдельный шаг (F4), нужный
лишь при росте apply-логики (второй портал, воронки).
"""
from typing import Any

from hrwork.application.apply.outcome import ApplyChannel
from hrwork.application.apply.runtime import bump_state, quota
from hrwork.infrastructure.storage import followup, load_marks, save_marks


class ApplicationStore:
    """Единая точка состояния откликов. Методы — тонкие делегаты (см. модуль-докстринг)."""

    # ── Отметки: какие вакансии откликнуты/отклонены (marks.json) ──
    @staticmethod
    def marks() -> dict[str, Any]:
        return load_marks()

    @staticmethod
    def set_marks(full: dict[str, Any]) -> None:
        """Полностью заменить карту отметок (лента шлёт свой актуальный набор)."""
        save_marks(full)

    @staticmethod
    def merge_marks(updates: dict[str, Any]) -> None:
        """Домержить отметки в текущие (не теряя чужих записей)."""
        save_marks({**load_marks(), **updates})

    @classmethod
    def mark_applied(cls, vid: str) -> None:
        cls.merge_marks({str(vid): "applied"})

    # ── Дневная квота откликов (apply_quota.json) ──
    @staticmethod
    def applied_today() -> int:
        return quota.applied_today()

    @staticmethod
    def daily_cap() -> int:
        return quota.DAILY_CAP_DEFAULT

    @staticmethod
    def bump_quota(n: int) -> int:
        return quota.bump_quota(n)

    # ── Кулдаун поднятия резюме (bump_state.json; лимит HH — раз в 4ч) ──
    @staticmethod
    def bump_due() -> bool:
        return bump_state.bump_due()

    @staticmethod
    def mark_bumped() -> None:
        bump_state.mark_bumped()

    @staticmethod
    def hours_since_bump() -> float | None:
        return bump_state.hours_since_bump()

    # ── Журнал откликов (applied_log.jsonl, append-only) ──
    @staticmethod
    def log_applied(vid: str, name: str, url: str, via: ApplyChannel,
                    status: str = "applied", ts: str = "", employer: str = "") -> None:
        followup.append_applied(vid, name, url, via=via.code, status=status, ts=ts,
                                employer=employer)

    @staticmethod
    def applied_log() -> list[dict[str, Any]]:
        return followup.load_applied_log()

    @classmethod
    def applied_ids(cls) -> set[str]:
        """id всех уже отправленных откликов (для дедупа при синке из чатов)."""
        return {str(e.get("id")) for e in cls.applied_log()}

    # ── Статусы работодателя из чатов (response_status.json) ──
    @staticmethod
    def statuses() -> dict[str, Any]:
        return followup.load_statuses()

    @staticmethod
    def save_statuses(statuses: dict[str, Any]) -> None:
        followup.save_statuses(statuses)

    # ── Вакансии-опросники: нужна ручная форма (form_vacancies.json) ──
    @staticmethod
    def forms() -> dict[str, Any]:
        return followup.load_form_vacancies()

    # ── Переписка в чатах (для подсветки «ждёт ответа» в ленте) ──
    @staticmethod
    def chat_messages() -> dict[str, Any]:
        return followup.load_chat_messages()

    @staticmethod
    def save_chat_messages(data: dict[str, Any]) -> None:
        followup.save_chat_messages(data)

    @staticmethod
    def add_form(vid: str, name: str, url: str, ts: str = "") -> None:
        followup.add_form_vacancy(vid, name, url, ts=ts)

    @staticmethod
    def remove_form(vid: str) -> None:
        followup.remove_form_vacancy(vid)

    # ── Кеш снятой структуры анкет (forms_cache.json): свип не гонит форму повторно ──
    @staticmethod
    def form_cache() -> dict[str, Any]:
        return followup.load_form_cache()

    @staticmethod
    def cache_form(vid: str, name: str, url: str, fields: list[Any],
                   status: str, ts: str = "") -> None:
        followup.cache_form(vid, name, url, fields, status, ts=ts)

    @staticmethod
    def cached_form_ids() -> set[str]:
        return followup.cached_form_ids()

    # ── Очередь ожидания: лента -> крон, когда браузер занят (apply_pending.json) ──
    @staticmethod
    def enqueue(vid: str, url: str, name: str, cover: str) -> int:
        return followup.enqueue_pending(vid, url, name, cover)

    @staticmethod
    def pop_pending() -> dict[str, Any] | None:
        return followup.pop_pending_one()

    @staticmethod
    def pending() -> list[dict[str, Any]]:
        return followup.load_pending()


# Состояние на диске — экземпляр без своего состояния; один общий на процесс.
store = ApplicationStore()
