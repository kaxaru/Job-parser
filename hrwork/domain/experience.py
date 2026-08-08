"""Value Object уровня опыта. Единый источник маппинга грейдов ЛЮБОГО портала -> опыт HH
(раньше — hirify._GRADE_TO_EXP/_GRADE_ORDER) и подписи (config.EXP_LABELS).

Словарь грейдов живёт ЗДЕСЬ, а не в адаптерах. Проверено на себе 07.08.2026: адаптеры
himalayas/jobicy/themuse завели по своей таблице маппинга, и один и тот же «senior» рисковал
попасть в разные корзины на разных порталах — а по нему идут и отбор под отклик, и срезы
аналитики. Адаптер обязан лишь ИЗВЛЕЧЬ ярлыки грейда из своей схемы; что они означают —
знает домен."""
from collections.abc import Iterable
from enum import Enum
from typing import Any

# КОМПРОМИСС (08.08.2026): доменный модуль тянет `hrwork.config` ради ОДНОГО словаря
# подписей EXP_LABELS (используется только в `Experience.label`). Импорт `config` не
# бесплатен: на нём делаются DATA_DIR.mkdir()/LOGS_DIR.mkdir(), load_dotenv,
# log.remove()+log.add() и чтение resume_profile.json — то есть `import
# hrwork.domain.experience` создаёт каталоги и переконфигурирует глобальный loguru,
# вопреки обещанию docs/domain.md «ни сети, ни диска».
# Оставлено сознательно: перенос подписей в домен раздвоил бы источник (подписи читает и
# конфиг-потребитель), а это тот же класс дефекта, что split-brain словарей FreshnessClass.
# Развязка — вынос чистых констант в модуль без side-effect'ов, см. docs/domain.md
# «Известные компромиссы».
from hrwork.config import EXP_LABELS


class Experience(Enum):
    NONE = "noExperience"       # hh_id = значение enum (совместимо с raw-схемой)
    BETWEEN_1_3 = "between1And3"
    BETWEEN_3_6 = "between3And6"
    MORE_6 = "moreThan6"

    @property
    def hh_id(self) -> str:
        return self.value

    @property
    def label(self) -> str:
        return EXP_LABELS[self.value]        # единый источник подписей

    @classmethod
    def from_code(cls, code: str | None) -> "Experience | None":
        """Ленивый парсер сохранённого experience.id / HH workExperience -> уровень.
        Пусто/неизвестный код -> None (опыт не указан). Единый контракт с Schedule.from_code:
        парсеры внешних данных возвращают VO|None, дефолт — на вызывающем."""
        try:
            return cls(code) if code else None
        except ValueError:
            return None

    @classmethod
    def from_grades(cls, names: Iterable[Any]) -> "Experience | None":
        """Ярлыки грейда С ЛЮБОГО портала -> уровень опыта. None, если ни один не узнан.

        Берётся самый МЛАДШИЙ из распознанных: вакансия с вилкой грейдов («Entry-Level,
        Junior», `levels: [mid, senior]`) открыта и для младшего, а очередь откликов
        сортируется от младших. Сравнение по ВХОЖДЕНИЮ подстроки — порталы пишут по-разному
        («Mid Level», «mid-level», «Midweight»), но это один и тот же грейд.

        Единственная точка, где ярлык превращается в уровень. Адаптеру остаётся достать
        список ярлыков из своей схемы — семантику знает домен."""
        raw = [str(n).strip().lower() for n in (names or []) if str(n).strip()]
        if not raw:
            return None
        for grade, exp in _GRADE_TO_EXP:        # порядок = от младшего к старшему
            if any(grade in n for n in raw):
                return exp
        return None

    @classmethod
    def from_hirify_grades(cls, grades: list[dict[str, Any]] | None) -> "Experience | None":
        """Самый МЛАДШИЙ грейд вакансии hirify -> уровень опыта (None, если грейдов нет)."""
        return cls.from_grades(g.get("name") for g in (grades or []))

    @classmethod
    def from_getmatch(cls, seniority: str | None,
                      years: int | None = None) -> "Experience | None":
        """Грейд getmatch (`seniority`) -> уровень опыта; при отсутствии — по числу лет
        (`required_years_of_experience`).

        Приоритет у ГРЕЙДА, а не у лет, хотя лет-поле точнее выглядит: словарь грейдов
        у getmatch тот же, что у hirify (trainee/junior/middle/senior/lead), и один и тот
        же «senior» с обоих порталов обязан попасть в одну корзину — иначе отбор
        кандидатов и срезы аналитики поедут между источниками. Замер 01.08.2026 на 14
        карточках: senior встречался с years=3, что по годам дало бы BETWEEN_1_3."""
        s = (seniority or "").strip().lower()
        for grade, exp in _GRADE_TO_EXP:
            if grade == s:
                return exp
        if years is None:
            return None
        if years <= 0:                          # границы совпадают с вилками HH
            return cls.NONE
        if years <= 3:
            return cls.BETWEEN_1_3
        if years <= 6:
            return cls.BETWEEN_3_6
        return cls.MORE_6


# Ярлык грейда -> Experience. ОБЩИЙ словарь для всех порталов, порядок = от младшего к
# старшему (его использует from_grades: побеждает первое совпадение).
#
# Диалекты сведены в одну таблицу СОЗНАТЕЛЬНО. hirify/getmatch говорят
# trainee/junior/middle/senior/lead, himalayas — Entry-level/Mid-level/Senior/Manager/
# Director/Executive, jobicy — Entry-Level/Junior/Midweight/Senior/Director, themuse —
# internship/entry/mid/senior/management. Пока каждый адаптер держал свою таблицу
# (07.08.2026), один и тот же «senior» мог попасть в разные корзины, а по грейду идут
# и отбор под отклик, и срезы аналитики.
#
# ПОРЯДОК ВАЖЕН, потому что побеждает ПЕРВОЕ совпадение, а сравнение идёт по вхождению
# подстроки. Отсюда два следствия:
#   * вилка грейдов («Entry-Level, Junior») отдаёт МЛАДШИЙ — ради этого порядок и выбран;
#   * ключи-подстроки друг друга («mid» внутри «middle»/«midweight») обязаны вести в ОДИН
#     уровень, иначе результат зависел бы от позиции. Сейчас все три -> BETWEEN_1_3;
#     добавляя ключ, проверь, не является ли он подстрокой соседа с другим уровнем.
#
# Эта таблица говорит, сколько лет опыта требует РАБОТОДАТЕЛЬ, и НЕ обязана совпадать с
# `grade.py::_BY_EXP` (какую свою вилку называть по годам — самооценка владельца профиля).
# Расхождение намеренное и разобрано в комментарии у `_BY_EXP`; его набор пришпилен стражем
# `test_grade.py::test_grade_label_roundtrip_matches_the_title_dictionary`. Добавляя сюда
# ключ, который встречается и в тайтлах (`grade.py::_TITLE_RX`), прогони этот тест.
_GRADE_TO_EXP: list[tuple[str, Experience]] = [
    ("trainee",     Experience.NONE),
    ("intern",      Experience.NONE),       # internship (themuse), intern (jobicy)
    ("entry",       Experience.NONE),       # entry-level — ДО «lead», см. выше
    ("junior",      Experience.BETWEEN_1_3),
    ("middle",      Experience.BETWEEN_1_3),
    ("midweight",   Experience.BETWEEN_1_3),   # jobicy
    ("mid",         Experience.BETWEEN_1_3),   # mid-level / Mid Level
    ("senior",      Experience.BETWEEN_3_6),
    ("lead",        Experience.MORE_6),
    ("principal",   Experience.MORE_6),        # talanto
    ("head",        Experience.MORE_6),        # talanto
    ("manager",     Experience.MORE_6),        # himalayas
    ("management",  Experience.MORE_6),        # themuse
    ("director",    Experience.MORE_6),
    ("executive",   Experience.MORE_6),
]
