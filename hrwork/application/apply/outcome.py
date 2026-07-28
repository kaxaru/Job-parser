"""Value Objects контекста «отклик»: исход попытки и канал.

Раньше — «магические строки» (`status == "applied"`, `via="cron"`), сравниваемые литералами
на пути реальных откликов: опечатка молча ломала бы запись в marks/quota. Enum даёт единый
язык и исчерпываемость. `.value`/`.code` = тот же код, что был строкой (диск/лог/wire
не меняются — как `Schedule.hh_code` в F1).

Транспортные состояния воркера (busy / no-session / queued / error) — НЕ здесь: это состояния
доставки (браузер/сессия/очередь), другой концепт, живут как wire-строки ответа /api/apply.
"""
from enum import Enum


class ApplyOutcome(Enum):
    """Исход одной попытки отклика (`apply_one`)."""
    APPLIED = "applied"    # отклик отправлен
    ALREADY = "already"    # уже откликались ранее (кнопки «Откликнуться» нет)
    FORM = "form"          # опросник/вопросы работодателя -> ручная форм-очередь
    SKIP = "skip"          # архив/внешний сайт/не подтвердилось
    CAPTCHA = "captcha"    # HH увёл на /account/captcha -> прогон ОСТАНАВЛИВАЕТСЯ целиком

    @property
    def code(self) -> str:
        """Код для wire/лога (тот же, что был строкой-литералом)."""
        return self.value


class ApplyChannel(Enum):
    """Канал, которым сделан отклик (пишется в журнал applied_log)."""
    CRON = "cron"          # крон-батч
    FEED = "feed"          # клик из ленты (serve)
    HH = "hh"              # ручной отклик на hh.ru, подхвачен синком из чатов

    @property
    def code(self) -> str:
        return self.value
