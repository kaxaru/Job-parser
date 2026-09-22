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

from hrwork.application.apply.outcome import ApplyChannel, VacancyMark
from hrwork.application.apply.runtime import bump_state, quota
from hrwork.infrastructure.storage import followup, load_marks, update_marks
from hrwork.infrastructure.storage.followup import JournalRow


class ApplicationStore:
    """Единая точка состояния откликов. Методы — тонкие делегаты (см. модуль-докстринг)."""

    # ── Отметки: какие вакансии откликнуты/отклонены (marks.json) ──
    @staticmethod
    def marks() -> dict[str, Any]:
        return load_marks()

    @staticmethod
    def set_marks(full: dict[str, Any]) -> None:
        """Заменить карту отметок набором ленты, НЕ теряя «откликнулись» с диска (RFC-004).

        Лента шлёт ВСЮ карту, загруженную когда-то раньше. Отметку «откликнулись», которую за это
        время записал крон, синк или другой аккаунт, лента не знает, и полная замена её стирала —
        а отбор снова видел вакансию свободной. Отклик необратим, поэтому такая отметка с диска
        подмешивается, если ленты про вакансию нечего сказать; своё значение ленты побеждает.
        Цена: снять «откликнулись» из ленты нельзя — ставила её либо автоматика по факту
        отклика, либо человек, и в обоих случаях отклик уже был."""
        applied = VacancyMark.APPLIED.code
        update_marks(lambda current: {**{k: v for k, v in current.items() if v == applied},
                                      **full})

    @staticmethod
    def merge_marks(updates: dict[str, Any]) -> None:
        """Домержить отметки в текущие (не теряя чужих записей, в том числе других процессов)."""
        update_marks(lambda current: {**current, **updates})

    @classmethod
    def mark_applied(cls, vid: str) -> None:
        # Код метки — из VO, а не литералом: `marks.py::load_marks` молча выбрасывает значение
        # не из `MARK_VALUES`, поэтому опечатка здесь не упала бы, а «забыла» отклик — и крон
        # откликнулся бы на ту же вакансию повторно.
        cls.merge_marks({str(vid): VacancyMark.APPLIED.code})

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

    @staticmethod
    def reconcile_quota(journaled_today: int) -> int:
        """Поднять сегодняшний счётчик до факта из журнала (только вверх, см. quota.py)."""
        return quota.reconcile_quota(journaled_today)

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
    def log_applied(row: JournalRow) -> None:
        """Дозаписать строку журнала. VO вместо восьми параметров (аудит 22.09.2026, §5):
        набор и порядок полей записи задаёт сама строка, а не порядок аргументов вызова."""
        followup.append_applied(row)

    # ── Единая точка фиксации УШЕДШЕГО отклика (аудит 22.09.2026, §1) ──
    def commit_applied(self, vid: str, *, name: str, url: str, employer: str,
                       via: ApplyChannel, ab: bool | None = None,
                       drop_form: bool = False) -> int:
        """Отметка -> квота -> журнал: одна последовательность для ВСЕХ путей отклика.

        До 22.09.2026 эта тройка была написана трижды (`autoclick::_apply_batch`,
        `autoclick::_apply_one_vacancy`, `forms::run`) и уже разошлась: feed-путь не писал
        `ab`, а порядок и состав полей приходилось сверять глазами. Класс инцидентов
        18.07/28.07 («отклик ушёл, а учёт не дошёл»): правку порядка или новое поле забывают
        в одной из копий — недосчитанная квота (упор в лимит HH), строка журнала без владельца
        доли (ломает наблюдаемость RFC-004) или потерянная отметка (повторный отклик).

        ПОРЯДОК ЗАПИСИ — как в крон-батче: отметка, затем квота, затем журнал. Он значим:
        квота и отметка дешевле журнала, а отметка обязана лежать на диске раньше всего —
        именно её отсутствие 18.07 дало повторные отклики после kill посреди прогона.

        `drop_form=True` — только форм-путь: анкета снимается с очереди ДО отметки, иначе
        убитый посреди прогона процесс вернёт уже отправленную анкету в оборот.

        `ab` — была ли вакансия в общей части A/B-сплита; None = неизвестно (лента и очереди
        не знают сплита). Поле всегда доезжает до `log_applied` одним и тем же набором
        параметров; сам журнал `None` не пишет (см. `followup.append_applied`).

        Возвращает `applied_today()` — значение счётчика ПОСЛЕ инкремента (как `bump_quota`)."""
        if drop_form:
            self.remove_form(str(vid))
        self.mark_applied(str(vid))
        total = self.bump_quota(1)
        self.log_applied(JournalRow(vid=str(vid), name=name, url=url, via=via.code,
                                    employer=employer, ab=ab))
        return total

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
    def add_form(vid: str, name: str, url: str, ts: str = "") -> bool:
        """True — анкета легла в очередь впервые, False — уже лежала (для счётчика прогона)."""
        return followup.add_form_vacancy(vid, name, url, ts=ts)

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
    def enqueue(vid: str, url: str, name: str, cover: str, employer: str = "") -> int:
        return followup.enqueue_pending(vid, url, name, cover, employer=employer)

    @staticmethod
    def pop_pending() -> dict[str, Any] | None:
        return followup.pop_pending_one()

    @staticmethod
    def requeue_pending(rec: dict[str, Any]) -> int:
        """Вернуть в очередь запись, снятую `pop_pending`, но не обработанную."""
        return followup.requeue_pending(rec)

    @staticmethod
    def pending() -> list[dict[str, Any]]:
        return followup.load_pending()


# Состояние на диске — экземпляр без своего состояния; один общий на процесс.
store = ApplicationStore()
