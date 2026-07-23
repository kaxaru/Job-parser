"""Value Object роли вакансии (по тайтлу).

Раньше — свободная строка на `Vacancy.role`, сравниваемая магическим литералом
`!= "Не-IT"` в агрегациях. Enum даёт единый язык и свойство `is_it` вместо литерала.
`.value`/`.label` = та же строка-ярлык, что была (feed отдаёт её в JS, чипы дашборда
упорядочены по config.ROLE_PATTERNS) — граница не меняется. Роль НЕ персистится
(вычисляется из techs при загрузке), поэтому сериализации Role нет — только feed->JS.

Значения ДОЛЖНЫ совпадать с ключами config.ROLE_PATTERNS (+ NON_IT/DEVELOPER — доменные
дефолты, не из регекс-карты). Дрейф ловит test_role (ROLE_PATTERNS ⊆ Role.labels)."""
from enum import Enum


class Role(Enum):
    MOBILE = "Mobile"
    QA = "QA"
    DEVOPS = "DevOps"
    DATA_ENG = "Data Eng"
    DATA_ML = "Data/ML"
    ANALYST = "Аналитик"
    EMBEDDED = "Embedded"
    SECURITY = "Security"
    GAMEDEV = "Gamedev"
    ARCHITECT = "Architect"
    DESIGNER = "Дизайнер"
    FRONTEND = "Frontend"
    BACKEND = "Backend"
    FULLSTACK = "Fullstack"
    MANAGER = "Менеджер"
    DEVELOPER = "Разработчик"    # фолбэк: тайтл роль не назвал, но есть язык
    NON_IT = "Не-IT"             # не-IT (водитель/ритейл/крауд-разметка/…)

    @property
    def label(self) -> str:
        """Ярлык для показа/JS-границы (тот же, что был строкой)."""
        return self.value

    @property
    def is_it(self) -> bool:
        """IT-роль? (всё, кроме NON_IT). Заменяет магический литерал `!= "Не-IT"`."""
        return self is not Role.NON_IT

    @classmethod
    def from_label(cls, label: str) -> "Role":
        """Строгий roundtrip ДОВЕРЕННОГО ярлыка (ключ ROLE_PATTERNS) -> Role. Raise на
        незнакомом — это не парсер внешних данных, а инверсия .label: fail-fast ловит дрейф
        config при импорте (см. parsing._ROLE_RX). Ленивый разбор внешнего входа — не сюда."""
        return cls(label)
