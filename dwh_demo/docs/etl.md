# ETL — конвейер

**Модули:** `etl/` — `domain.py`, `source.py`, `pipeline.py`, `config.py`, `cli.py`.

## Назначение

Прочитать JSON парсера, привести к типизированной модели, разложить по хранилищам.
Extract и transform выполняются **один раз**, load — в каждый инжектированный бэкенд.

## Домен — `domain.py`

Чистый слой: ни БД, ни I/O. Переиспользуется всеми адаптерами и тестируется без контейнеров.

**`Vacancy`** — frozen dataclass, нейтральное представление, общий контракт для всех
хранилищ:

```python
id: str                        # неймспейс по источникам (hirify_/talanto_<uuid>) — не int
source: str                    # портал: hh | hirify | talanto (из raw-поля _source)
name: str
city: str | None
employer: str | None
salary_min / salary_max: float | None
salary_currency: str | None
salary_gross: bool | None
experience: str | None        # уже человекочитаемо: «1–3 года»
schedule: str | None          # «Удалённо», «Полный день», …
is_remote: bool
url: str | None
query: str | None             # из какого поискового запроса пришла
skills: tuple[str, ...]
```

### Контракт `Vacancy.from_raw`

Вход — сырая запись парсера, выход — типизированная вакансия:

```python
>>> Vacancy.from_raw({
...     "id": "135125416",
...     "name": "Python-разработчик",
...     "area": {"id": "1", "name": "Москва"},
...     "salary": {"from": 150000, "to": None, "currency": "RUR", "gross": False},
...     "experience": {"id": "noExperience"},
...     "schedule": {"id": "fullDay"},
...     "snippet": {"requirement": "Опыт с Python, Docker и PostgreSQL"},
...     "employer": {"name": "НПП МетаСофт Про"},
...     "alternate_url": "https://hh.ru/vacancy/135125416",
... })
Vacancy(id='135125416', source='hh', name='Python-разработчик', city='Москва',
        employer='НПП МетаСофт Про', salary_min=150000, salary_max=None,
        salary_currency='RUR', salary_gross=False,
        experience='Без опыта', schedule='Полный день', is_remote=False,
        url='https://hh.ru/vacancy/135125416', query=None,
        skills=('Python', 'PostgreSQL', 'Docker'))
```

Что произошло, и это видно только на паре вход/выход:

- **`id` -> `str`** — основной проект неймспейсит id по источникам (`hirify_733072`,
  `talanto_<uuid>`), поэтому `int()` больше неприменим (ронял 2/3 записей в except).
  Ключи хранилищ — TEXT/String/NVARCHAR
- **`source`** — портал (`hh`/`hirify`/`talanto`) из служебного поля `_source`; дефолт
  `hh` для legacy-срезов. Даёт разрез «источник» в core и витрину `mart.source_stats`
- **коды нормализованы**: `noExperience` -> «Без опыта», `fullDay` -> «Полный день»
  (справочники `EXPERIENCE`, `SCHEDULE`). Неизвестный код проходит **как есть**,
  а не превращается в `None`
- **`skills`** — 37 regex по объединённому тексту (название + requirement + описание
  без HTML). Возвращается кортеж, а не список: `Vacancy` фrozen
- **`is_remote`** — либо `schedule == "remote"`, либо один из `REMOTE_MARKERS` в тексте
  («удалённ», «remote», «из любой точки», …)
- **`city`** берётся из `area.name`, с фолбэком на `_city` парсера

**`strip_html`** — снимает теги и `&nbsp;`-сущности перед разметкой навыков, иначе
разметка ловила бы совпадения внутри атрибутов.

## Источник — `source.py`

`JsonSource(path).read() -> list[dict]`. Один источник, но контракт позволяет добавить
другие (API, БД) не трогая конвейер.

## Конвейер — `pipeline.py`

```python
Pipeline(source, warehouses)
  .prepare()      -> list[Vacancy]   extract + transform, кэшируется
  .init_schema()                      по всем хранилищам
  .load()                             по всем хранилищам
  .run(steps)                         steps: пусто | ["all"] | ["init"] | ["load"]
```

**`build_pipeline(targets, cfg)`** — фабрика: собирает источник и адаптеры выбранных
бэкендов из `REGISTRY`.

### Дедуп и отбраковка в `prepare`

```python
if not r.get("id"):        continue    # запись без id
try:    v = Vacancy.from_raw(r)
except (KeyError, ValueError, TypeError): continue   # битая запись
if v.id in seen:           continue    # дубль
```

Дедуп обязателен: одна вакансия приходит из нескольких поисковых запросов (`_query`),
и дубль по id **уронил бы** PRIMARY KEY в PostgreSQL и MS SQL, а в ClickHouse молча
задвоил бы счётчики.

Битые записи пропускаются молча, а не роняют прогон: источник внешний, одна кривая
запись из десяти тысяч не повод терять загрузку.

**Кэширование `prepare`.** Результат считается один раз и переиспользуется всеми
хранилищами. Если бы каждый адаптер парсил сам, три движка могли бы разойтись в данных
при одинаковом входе — и сравнение потеряло бы смысл.

## CLI — `cli.py`

```bash
python -m etl                      # = all: init схем + load во все бэкенды
python -m etl all
python -m etl init                 # только схемы (идемпотентно)
python -m etl load                 # только загрузка

python -m etl -t postgres all      # только Postgres
python -m etl -t clickhouse load
python -m etl -t postgres -t mssql init    # несколько — флаг повторяется
python -m etl --help
```

Ожидаемый вывод:

```
[etl] [postgres] schema ready
[etl] [clickhouse] schema ready
[etl] [mssql] schema ready
[etl] prepare: 92730 вакансий (extract+transform)
[etl] [postgres] loaded: 92730
[etl] [clickhouse] loaded: 92730
[etl] [mssql] loaded: 92730
```

Совпадение чисел по всем бэкендам — главная проверка согласованности адаптеров.

### Две тонкости argparse, зафиксированные в коде

**`action="append"` вместо `nargs="+"`** для `--target`: иначе опция жадно съедает
позиционный `STEP`, и `-t postgres load` ломается.

**Валидация шага через `type`, а не `choices`**: связка `choices` + `nargs="*"` в argparse
валидирует пустой список против `choices` и падает на пустом вводе (bpo-9625).

## Крайние случаи

- **Пустой список шагов** -> трактуется как `all` (`Pipeline.run`)
- **Запись без `id`** -> пропуск
- **Битая запись** -> пропуск, без падения прогона
- **Неизвестный код опыта/графика** -> проходит как есть
- **Нет файла данных** -> `FileNotFoundError` из `JsonSource.read`, прогон падает —
  это не деградация, а отсутствие входа

## Проверка

```bash
python -m pytest tests/test_domain.py tests/test_pipeline.py tests/test_cli.py -q
python -m etl -t postgres all
```
