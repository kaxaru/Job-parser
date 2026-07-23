# Доменный слой

**Слой:** `hrwork/domain/` — ядро. Ни от чего не зависит, кроме `config`. Ни сети, ни диска,
ни HTML.

## Назначение

Единый язык предметной области «вакансия» и правила, которые не зависят от источника данных.
HH, hirify и talanto отдают разные форматы; домен знает только один — свой.

## Сущность `Vacancy`

`domain/models.py::Vacancy` — dataclass, mutable.

Поля: `id`, `name`, `city`, `city_id` · `salary: Salary | None` (уже **net**) ·
`experience: Experience | None` · `schedule: Schedule` (обязателен) · `techs: list[str]` ·
`role: Role` · `employer` · `created_at` / `published_at` (ISO) · `responses: int | None` ·
`source`.

Поведение — методами на сущности (`models.py::Vacancy`), а не в сервисах:
`age_days()`, `fresh_class()`, `is_ghost()`, `republish_gap_days()`, `is_remote()`.

`is_remote()` — строго `schedule is Schedule.REMOTE`. Текстовые маркеры удалёнки
(`parsing.has_remote`) на сущности **не живут**: это эвристика по описанию, а не факт из
структурированного поля.

### Пример реальной сущности

Вход — запись из `vacancies_raw.json` (см. [`storage.md`](storage.md)), выход —
результат `parsing.py::parse_vacancy`:

```python
Vacancy(
    id="135125416",
    name="Python-разработчик",
    city="Москва", city_id="1",
    salary=Salary(frm=150000, to=None, currency="RUR", gross=False),  # уже net
    experience=Experience.NONE,          # "noExperience"
    schedule=Schedule.OFFICE,            # "fullDay"
    techs=["Python", "ML/AI"],           # детекция по name + snippet
    role=Role.DEVELOPER,                 # "Разработчик"
    employer="НПП МетаСофт Про",
    created_at="2026-07-13T12:04:03.511+03:00",
    published_at="2026-07-19T12:04:03.511+03:00",   # переоткрыта через 6 дней
    responses=432,
    source="hh",
)
```

Производные значения на этой записи:

```python
v.age_days()             # 6      — по created_at
v.fresh_class()          # FreshnessClass.FRESH
v.republish_gap_days()   # 6      — published_at минус created_at
v.is_remote()            # False  — schedule=OFFICE
v.is_ghost()             # False
```

Три вещи, которые видно только на примере: `to=None` при заданном `frm` — валидная вилка;
`gross=False` означает, что вычет НДФЛ **уже применён** и повторно не применяется;
`role` не хранится в файле, а вычисляется заново при каждой загрузке.

## Value Objects

Примитивы вместо VO — главный источник багов в этом проекте, поэтому каждое доменное
понятие имеет тип.

### `Salary` — `domain/salary.py::Salary`

Frozen VO: `frm`, `to`, `currency`, `gross`.

- `from_raw(dict | None) -> Salary | None` — `None`, если **обе** границы `None`.
  Проверка через `is None`, не truthiness: вилка `from=0` валидна.
- `.mid -> int | None` — среднее либо единственная граница.
- `.net()` — gross -> net через `NET_FROM_GROSS = 0.87`; идемпотентен.
- `.net_triple()` -> `(frm, to, mid)`.

### `Experience` — `domain/experience.py::Experience`

Enum, значения = HH-коды (`noExperience` / `between1And3` / `between3And6` / `moreThan6`).
`from_hirify_grades()` берёт **самый младший** грейд из списка.

### `Schedule` — `domain/schedule.py::Schedule`

`REMOTE` / `HYBRID` / `OFFICE`. `from_hh_formats` и `from_hirify_wf` схлопывают список
форматов по приоритету REMOTE > HYBRID > OFFICE.

### `Role` — `domain/role.py::Role`

17 ролей; значения обязаны совпадать с ключами `config.ROLE_PATTERNS`.
`.is_it` = `self is not Role.NON_IT`.

### `FreshnessClass` — `domain/freshness.py::FreshnessClass`

`fresh` / `recent` / `ghost` / `unknown`. Пороги `FRESH_DAYS = 30`, `GHOST_DAYS = 60`.

Enum владеет **всеми** проекциями: `.code`, `.label`, `.color`, `.order`. Раньше подписи
жили в `freshness.py`, а цвета в `config.py`, и словари молча разошлись — у цветового не
было ключа `unknown`, что давало латентный `KeyError`.

## Два контракта парсинга — выбирать явно

Это главное правило слоя. Оба контракта нужны, путать их нельзя.

**Мягкий парсер** — для внешних данных, которым нельзя доверять:

```python
Experience.from_code(code) -> Experience | None   # неизвестное -> None
Schedule.from_code(code)   -> Schedule | None
```

Дефолт применяет вызывающий: `Schedule.from_code(...) or Schedule.OFFICE`
(`parsing.py::parse_vacancy`). Домен не решает за ACL, что значит «поле отсутствует».

**Строгий roundtrip** — для наших собственных констант, где расхождение = баг:

```python
Role.from_label(label)         -> Role            # БРОСАЕТ ValueError
FreshnessClass.from_code(code) -> FreshnessClass  # БРОСАЕТ
```

Это инверсия `.label`, а не парсер. Работает как fail-fast: `_ROLE_RX` резолвит все ключи
`ROLE_PATTERNS` **на импорте** `parsing.py` (`parsing.py::_ROLE_RX`), поэтому опечатка в конфиге
роняет процесс сразу, а не через час сбора неправильной классификацией.

## Бизнес-правила классификации

`domain/parsing.py` — ACL и детекция. Роль считается **по тайтлу**, не по описанию: тело
вакансии упоминает «тестирование», «безопасность», «ML» в проходном контексте, и backend
уезжал в QA.

### Порядок `_detect_role` (`parsing.py::_detect_role`)

1. `_HARD_NON_IT` -> `NON_IT` **безусловно**
2. `_GIG_NON_IT` (крауд-разметка: `ai-тренер`, `асессор`, `толок`, `разметчик`) -> `NON_IT`
3. `_CREATIVE_NON_IT` (продюсер/режиссёр/монтажёр/видеограф/SMM/маркетолог/контент/копирайтер) ->
   `NON_IT`, **если языка нет в ТАЙТЛЕ**: креатив с баззвордом «AI» — не инженер (БАГ 21.07:
   «Продюсер AI видео» ловился `Data/ML` через `\bai\b` в `ROLE_PATTERNS` и уходил в отклики;
   БАГ 22.07: язык из ОПИСАНИЯ открывал гейт — «желательно Python» в JD продюсера промотил
   его в Data/ML, теперь гейт открывает только язык в самом названии)
4. `_SOFT_NON_IT` (`хими|технолог|конструктор|рецептур`) -> `NON_IT`, **только если техов нет**
5. первый матч по `_ROLE_RX` -> роль
6. фолбэк `Role.DEVELOPER` — только если среди техов есть **язык** (`config.LANG_KEYS`),
   иначе `NON_IT`

Уровни жёсткости — не украшение: «конструктор» без техов почти всегда инженер-механик, а с
техами — вероятно, конструктор интерфейсов; «продюсер» без языка — креатив, с языком — dev.

### `_HARD_NON_IT` (`parsing.py::_HARD_NON_IT`)

Один большой regex, собранный по живым ложным срабатываниям. Тематические блоки:
транспорт/склад/ритейл · поддержка/преподавание/продажи · физбезопасность и жизнеобеспечение ·
финаудит · юристы/HR/маркетинг · медиа (рилс/монтажёр/SMM) · нефтегаз/бухгалтерия · SEO ·
педагог/методист · студенческие работы · синие воротнички.

Правило пополнения: паттерн целится в **роль-существительное**, а не в домен. «Аудитор
смарт-контрактов» и «Программист для автоматизации SEO» обязаны проходить — исключения
проверены тестами.

`is_hard_non_it(name)` (`parsing.py::is_hard_non_it`) вызывается **до** enrich, чтобы не качать карточки
заведомо чужих вакансий.

### Детекция техов (`_detect_techs`, `parsing.py::_detect_techs`)

Двухступенчато. `_build_screened()` на импорте разбирает каждый паттерн по `|` и извлекает
гарантированный ведущий литерал. Если **все** альтернативы дают ядро >= 2 символов — сначала
дешёвый `str.__contains__`, потом regex. Иначе пресин отключается и regex зовётся всегда:
корректность важнее оптимизации.

Замерено: 0 расхождений на 34k вакансий, 137 c -> 8 c.

Перед детекцией `_strip_device_req` (`parsing.py::_strip_device_req`) вырезает клаузу «нужен смартфон на
iOS/Android» — иначе расклейщик листовок получал тег Android и фолбэк «Разработчик».

## Фабрики

- `build_vacancy(...)` (`parsing.py::build_vacancy`) — единая точка детекции стека и роли для **всех**
  ACL. Новый источник обязан звать её, а не собирать `Vacancy` вручную.
- `parse_vacancy(raw: dict)` (`parsing.py::parse_vacancy`) — persisted-dict -> домен.

## Крайние случаи

- **`Salary.from_raw` с `from=0`** -> валидная вилка (строгая проверка `is None`).
  Но `.net()` использует truthiness `if self.frm` — вилка `frm=0, gross=True` потеряет нижнюю
  границу. Известная асимметрия, в проде не встречалась (нулевых вилок в выдаче нет).
- **Неизвестный код опыта/формата** -> `None`, дефолт ставит вызывающий.
- **Дрейф `ROLE_PATTERNS`** -> `ValueError` на импорте, не тихая деградация.
- **Битая дата** (`parse_dt`) -> `None` -> `FreshnessClass.UNKNOWN`, не исключение.
- **«Сегодня»** инжектируется параметром (`age_days(created, today=None)`) — иначе тесты
  зависели бы от календаря.

## Проверка

```
pytest tests/backend/domain -q
```

Ключевые свойства под тестами: не-IT фильтр не съедает «Аудитор смарт-контрактов»;
`from_raw` не теряет нулевую границу; `Role.from_label` бросает на неизвестном ярлыке;
классификация свежести детерминирована при инжекте `today`.
