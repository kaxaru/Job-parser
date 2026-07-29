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
