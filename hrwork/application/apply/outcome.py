"""Value Objects контекста «отклик»: исход попытки и канал.

Раньше — «магические строки» (`status == "applied"`, `via="cron"`), сравниваемые литералами
на пути реальных откликов: опечатка молча ломала бы запись в marks/quota. Enum даёт единый
язык и исчерпываемость. `.value`/`.code` = тот же код, что был строкой (диск/лог/wire
не меняются — как `Schedule.hh_code` в F1).

Транспортные состояния воркера (busy / no-session / queued / error / taken) — отдельный
концепт «состояние доставки» (браузер/сессия/очередь), поэтому у них СВОЙ тип
(`TransportStatus`), а не члены `ApplyOutcome`. Именованы 22.09.2026 (аудит `2026-09-22-quality.md`,
§3.2): до этого жили строками в `server.py::_apply_post` и в `src/feed/main.js::APPLY_LABELS`,
и дрейф уже случился — сервер отдавал `taken`, метки для которого в ленте не было.

`APPLY_LABELS` — подписи для ленты (инжектятся в `feed-data.js`, мост `feed.py::build_feed`);
единственный источник текста для обоих типов сразу, полнота закрыта стражем
`tests/backend/application/test_outcome_labels.py`.
"""
from enum import Enum


class ApplyOutcome(Enum):
    """Исход одной попытки отклика (`apply_one`)."""
    APPLIED = "applied"    # отклик отправлен
    ALREADY = "already"    # уже откликались ранее (кнопки «Откликнуться» нет)
    FORM = "form"          # опросник/вопросы работодателя -> ручная форм-очередь
    SKIP = "skip"          # откликнуться НЕЛЬЗЯ: архив, внешний сайт, опросник
    UNCONFIRMED = "unconfirmed"  # клик был, подтверждения от HH нет (окно 24ч / disabled сабмит)
    CAPTCHA = "captcha"    # HH увёл на /account/captcha -> прогон ОСТАНАВЛИВАЕТСЯ целиком

    @property
    def code(self) -> str:
        """Код для wire/лога (тот же, что был строкой-литералом)."""
        return self.value


class TransportStatus(Enum):
    """Состояние доставки отклика (ответ `/api/apply` и очередь ожидания) — НЕ исход попытки."""
    QUEUED = "queued"          # положен в очередь ожидания (браузер занят)
    BUSY = "busy"              # занят lock: идёт другой браузерный прогон
    NO_SESSION = "no-session"  # нет/протухла сессия HH
    TAKEN = "taken"            # вакансию уже взял другой аккаунт (RFC-004)
    ERROR = "error"            # наша ошибка на пути отклика

    @property
    def code(self) -> str:
        return self.value


# Подписи для ленты: ключ — код исхода ИЛИ транспортного статуса. Один источник с 22.09.2026.
APPLY_LABELS: dict[str, str] = {
    ApplyOutcome.APPLIED.code: "✅ Отклик отправлен",
    ApplyOutcome.ALREADY.code: "уже откликались",
    ApplyOutcome.FORM.code: "📝 нужна форма — в очереди",
    ApplyOutcome.SKIP.code: "✖ пропущено (внешний/архив/опросник)",
    # Клик по «Откликнуться» был, HH его не подтвердил. Отдельная подпись обязательна:
    # 23.09.2026 серия из 33 таких отказов шла под общим ярлыком skip и была неотличима
    # от «откликнуться нельзя» — ни в ленте, ни счётчиком прогона.
    ApplyOutcome.UNCONFIRMED.code: "⚠ не подтвердилось — клик ушёл, ответа HH нет",
    ApplyOutcome.CAPTCHA.code: "⛔ капча HH — нужен вход руками",
    TransportStatus.QUEUED.code: "➕ в очереди крона",
    TransportStatus.BUSY.code: "⏳ занято — идёт крон-отклик, попробуйте через пару минут",
    TransportStatus.NO_SESSION.code: "⚠ нет сессии — hh.py autoclick --login",
    TransportStatus.TAKEN.code: "🚫 вакансию уже взял другой аккаунт",
    TransportStatus.ERROR.code: "⚠ ошибка",
}


class VacancyMark(Enum):
    """Метка вакансии в ленте (`marks.json`) — что с ней уже произошло.

    Значений ровно два (в данных: applied 1636, rejected 349), но литералы `"applied"` были
    размазаны по autoclick и forms, причём в forms — дважды дословно одним и тем же кортежем.
    Тот же случай, что описан в шапке модуля: сравнение строк на пути реальных откликов, где
    опечатка молча ломает логику «уже трогали — не шлём».

    В JS остаются литералы: это wire-формат ленты, как и `ApplyOutcome.code`."""
    APPLIED = "applied"      # отклик отправлен
    REJECTED = "rejected"    # работодатель отказал

    @property
    def code(self) -> str:
        return self.value


# вакансию больше не трогаем: отклик уже ушёл или пришёл отказ
SETTLED_MARKS = frozenset({VacancyMark.APPLIED.code, VacancyMark.REJECTED.code})


class ApplyChannel(Enum):
    """Канал, которым сделан отклик (пишется в журнал applied_log)."""
    CRON = "cron"          # крон-батч
    FEED = "feed"          # клик из ленты (serve)
    HH = "hh"              # ручной отклик на hh.ru, подхвачен синком из чатов

    @property
    def code(self) -> str:
        return self.value
