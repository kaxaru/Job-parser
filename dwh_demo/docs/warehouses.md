# Хранилища — три адаптера

Заменяет привычный `database.md`: здесь три движка, и половина ценности проекта в том,
чем именно они отличаются.

## Порт

`warehouse/base.py::Warehouse` — `Protocol`, помеченный `@runtime_checkable`:

```python
name: str
init_schema() -> None
load(vacancies: list[Vacancy]) -> int
count() -> int
```

`Pipeline` работает только с этим контрактом. Адаптеры не наследуются от базового класса —
структурная типизация, соответствие проверяется тестом.

**`load` возвращает число строк В ФАКТЕ** — прочитанное из хранилища (`SELECT count(...)`),
а не длину входа и не число отправленных строк. На этом числе держится главная проверка
проекта: `Pipeline._verify` сверяет его с результатом `prepare` по каждому движку.
Адаптер, возвращающий `len(vacancies)`, делает сверку вакуумной — она совпадёт, что бы
ни случилось в базе (так и было в ClickHouse до 09.08.2026).

**`count`** кроме сверки служит базой санити-гейта `Pipeline._reject_degraded`: перезалив
отменяется, если новый срез просел относительно факта больше чем вдвое.

**Политику восстановления адаптер обязан описать в докстринге `load`** — атомарна ли
замена факта и безопасен ли повтор после обрыва. Она у трёх движков **разная**, сводка —
в разделе «Сравнение».

## Общая модель данных

Одинакова для PostgreSQL и MS SQL, различается у ClickHouse.

**staging** — типизированный приём, 16 колонок:

```
staging.stg_vacancies (id, source, name, city_name, employer_name,
                       salary_min, salary_max, salary_min_rub, salary_max_rub,
                       salary_currency, salary_gross, experience, schedule,
                       is_remote, remote_mentioned, url)
staging.stg_skills    (vacancy_id, skill)
```

`id` — TEXT/String/NVARCHAR(200) (не BIGINT/UInt64): основной проект неймспейсит id
по источникам (`hirify_733072`, `talanto_<uuid>`), а himalayas и arbeitnow — слагом
вакансии. `source` — портал; перечень ведёт родитель (`hrwork/config.py::SOURCES`,
сейчас девять), схемы его не ограничивают.

**Зарплата хранится дважды, и это не дубль.** `salary_min`/`salary_max` — суммы **в валюте
портала** (в срезе 45 кодов, доллар встречается чаще рубля): их можно показать в карточке
вакансии, но нельзя складывать и усреднять. `salary_min_rub`/`salary_max_rub` — та же вилка,
приведённая к рублям **один раз в домене** (`domain.py::Vacancy.from_raw` -> `rates.to_rub`);
витрины только усредняют готовые рубли. До 09.08.2026 рублёвых колонок не было, и витрины
складывали доллары с сумами — «средняя зарплата по опыту» была числом без смысла.
Курс неизвестен или валюты нет -> **NULL**: вакансия выпадает из зарплатного среза, а не
считается нулём (тот же контракт, что у родителя).

**`remote_mentioned`** — словесный маркер удалёнки в тексте, **отдельная** колонка рядом
с `is_remote` (формат работы: коды `remote` + `flexible`). Смешаны они были до 09.08.2026,
и офисная вакансия со словом «удалённо» в описании попадала в долю удалёнки.

**Колонки `query` в схемах больше нет** — родитель это поле не пишет, колонка была
гарантированно NULL. На поднятом томе `CREATE TABLE IF NOT EXISTS` её бы не убрал, поэтому
в каждой из трёх схем стоит идемпотентный `DROP COLUMN IF EXISTS query`; тем же способом
на живой том доезжают рублёвые колонки и `remote_mentioned`.

**core** — звезда:

```
core.vacancies       факт: id, source, name, city_id, employer_id, зарплата (нативная
                     и рублёвая), опыт, schedule, is_remote, remote_mentioned, url, loaded_at
core.cities          измерение (id, name UNIQUE)
core.employers       измерение (id, name UNIQUE)
core.skills          измерение (id, name UNIQUE)
core.vacancy_skills  мост (vacancy_id, skill_id) — многие-ко-многим
```

Схлопывание измерений — **побайтовое, регистр значим**: «ACME» и «Acme» становятся двумя
работодателями. Эталон задаёт PostgreSQL (TEXT UNIQUE), ClickHouse сравнивает String так же,
а MS SQL приведён к ним явной коллацией — см. его раздел.

**mart — набор витрин по движкам НЕ одинаков**, и обещание «одна витрина = одно число»
действует только там, где витрина есть у обоих:

```
city_stats · source_stats · salary_by_experience · skill_demand · top_employers   PostgreSQL
city_stats · source_stats · salary_by_experience · skill_demand · top_employers   MS SQL
             source_stats · salary_by_exp        · skill_demand                   ClickHouse
```

В ClickHouse `city_stats` и `top_employers` нет **осознанно**: CH живёт в стенде ради
сравнения «один BI, два движка» на витринах, где интересна скорость агрегации широкого
факта (навыки через ARRAY JOIN, зарплата по опыту), а измерения-справочники (2 789 локаций,
28 380 работодателей) в звезду CH не выносятся. Обратная сторона: любая карточка «города»
или «работодатели» существует только для PG и MS SQL. То же соответствие продублировано
комментарием в самих схемах.

### Спецификация метрик (общая на три движка)

- Все зарплатные метрики считаются по `salary_*_rub` и **называются** с суффиксом `_rub`:
  человек в Metabase должен видеть единицу измерения, не открывая SQL
- **Обе средние берутся по одному множеству строк** — где известны **обе** рублёвые
  границы. Его размер и есть `with_salary_rub`. Иначе серии графика «вилка» считаются
  по разным выборкам: односторонних «от X» в срезе 8 948, «до Y» — 2 116, и верхний
  столбик может оказаться ниже нижнего
- **`with_salary` — это покрытие вилкой в любой валюте**, метрика полноты данных,
  а не знаменатель средних. Две колонки живут рядом намеренно: они отвечают на разные
  вопросы («сколько вакансий вообще называют деньги» против «по скольким мы смогли
  посчитать среднее»), и слияние их в одну снова сделает вилку несопоставимой
- **Округление — `round` (ничьи от нуля), не усечение.** Единственное признанное
  расхождение движков — ClickHouse, см. «Сравнение»
- Бакеты «не указан» (опыт) и «не указана» (локация) ловят и NULL, и **пустую строку**
  (`nullif(...,'')` перед `coalesce`). Домен пустой код уже отдаёт как `None`, но витрина
  обязана быть устойчивой к обоим представлениям: до 09.08.2026 голый `coalesce`
  не срабатывал никогда, и 20 650 вакансий (19 %) уходили в бакет с пустой подписью.
  Подписи продублированы в SQL трёх схем и в `domain.py` (`NO_EXPERIENCE_LABEL`,
  `NO_CITY_LABEL`) — SQL импортировать не умеет, равенство стережёт тест

Пример определения витрины (`sql/postgres/schema.sql`):

```sql
DROP MATERIALIZED VIEW IF EXISTS mart.city_stats;
CREATE MATERIALIZED VIEW mart.city_stats AS
SELECT coalesce(c.name, 'не указана') AS city, count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary,
       count(*) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL) AS with_salary_rub,
       round(avg(v.salary_min_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_min_rub,
       round(avg(v.salary_max_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_max_rub,
       round(100.0 * count(*) FILTER (WHERE v.is_remote) / count(*), 1) AS remote_share_pct
FROM core.vacancies v LEFT JOIN core.cities c ON c.id = v.city_id
GROUP BY coalesce(c.name, 'не указана');
```

Две детали видны только здесь. Витрины **пересоздаются** (`DROP` + `CREATE`), а не
`CREATE ... IF NOT EXISTS`: иначе правка определения доезжает в MS SQL немедленно
(`CREATE OR ALTER VIEW`), а в PostgreSQL — никогда, и движки расходятся навсегда при
зелёной проверке идемпотентности. И джойн на измерение — `LEFT`: локация неизвестна —
это бакет, а не выпадение из среза; `INNER JOIN` здесь молча терял 1 301 вакансию,
и сумма `vacancies` по `city_stats` не сходилась с `source_stats` на одном дашборде.

---

## PostgreSQL

**Модуль.** `warehouse/postgres.py::PostgresWarehouse`
**Драйвер.** `psycopg2` + `execute_values`
**Схема.** `sql/postgres/schema.sql`

**Как грузит.** `execute_values` многострочными INSERT по `BATCH_SIZE = 1000` в staging,
затем один пакет SQL (`postgres.py::LOAD_SQL`): справочники -> факт -> мост -> REFRESH витрин.

```sql
INSERT INTO core.cities(name) SELECT DISTINCT city_name FROM staging.stg_vacancies
  WHERE city_name IS NOT NULL ON CONFLICT (name) DO NOTHING;
TRUNCATE core.vacancy_skills, core.vacancies;
INSERT INTO core.vacancies(...) SELECT ... FROM staging.stg_vacancies s
  LEFT JOIN core.cities c ON c.name = s.city_name ...;
REFRESH MATERIALIZED VIEW mart.city_stats;
```

**Витрины.** Материализованные, обновляются явным `REFRESH` в конце load.

**Идемпотентность.** `TRUNCATE` факта и моста перед вставкой; измерения добираются
через `ON CONFLICT DO NOTHING` — накопительно, id сохраняются между прогонами.

**Политика восстановления: целиком или никак.** `TRUNCATE` staging, вставка батчей
и `LOAD_SQL` (внутри — `TRUNCATE core.*` и `REFRESH`) идут **в одной транзакции**:
исключение -> `ROLLBACK`, в факте остаётся предыдущий срез. Повтор безопасен.

**Edge cases**

- `LEFT JOIN` на измерения: вакансия без города или работодателя не теряется,
  получает `NULL` в `city_id`
- `loaded_at` со `DEFAULT now()` — отметка времени загрузки, единственное поле,
  добавляемое хранилищем
- Индексы по `city_id`, `employer_id`, `is_remote`, `source` создаются в схеме
- Соединение закрывается явно (`_session`): у psycopg2 `with conn` управляет
  **транзакцией**, а не жизненным циклом соединения. Без `close()` соединения копились бы
  в долгоживущем воркере Airflow до сборки мусора, а с 09.08.2026 их стало больше —
  санити-гейт зовёт `count()` перед каждой загрузкой

**Почему так.** `ON CONFLICT DO NOTHING` вместо пересоздания измерений: их id ссылаются
из моста, и пересборка ломала бы связи. Факт при этом перезаливается целиком — так проще
и данные малы.

---

## ClickHouse

**Модуль.** `warehouse/clickhouse.py::ClickHouseWarehouse`
**Драйвер.** нет — HTTP через `urllib` из stdlib
**Схема.** `sql/clickhouse/schema.sql`

**Модель отличается: широкий факт вместо звезды.**

```
hh.vacancies      ENGINE = MergeTree
                  id, source, name, city, employer, зарплата (нативная и рублёвая),
                  experience, schedule, is_remote, remote_mentioned, url,
                  skills Array(String)
hh.skill_demand   ENGINE = AggregatingMergeTree  <- hh.skill_demand_mv
hh.salary_by_exp  ENGINE = AggregatingMergeTree  <- hh.salary_by_exp_mv
hh.source_stats   ENGINE = AggregatingMergeTree  <- hh.source_stats_mv (объём/зарплата/remote × источник)
```

Навыки лежат массивом в самой строке, отдельных таблиц-измерений и моста нет. Для
колоночной СУБД джойны дороже, а массив разворачивается через `arrayJoin` — это идиома
движка, а не упрощение.

**Как грузит.** `TRUNCATE` факта и витрин, затем батчи по `BATCH = 2000` строк в формате
`JSONEachRow`:

```python
self._post("INSERT INTO hh.vacancies FORMAT JSONEachRow", "\n".join(batch))
```

**Витрины.** Инкрементальные MV поверх `AggregatingMergeTree` — наполняются **на каждой
вставке**, `REFRESH` не существует как понятие. Витрина хранит **состояния**
(`AggregateFunction`), а не числа: доля remote и любое округление считаются потребителем
на `avgMerge`/`sumMerge`/`countMerge`.

**Политика восстановления: атомарным этот адаптер быть не может.** В ClickHouse нет
транзакции на несколько запросов, поэтому `TRUNCATE` + батчи — не одна операция, и обрыв
оставляет факт **частично залитым**, в отличие от PG и MS SQL. Компенсируется двумя
свойствами: повтор восстанавливает всё (следующий прогон снова чистит и заливает срез
целиком), а частичный результат виден сразу — адаптер сверяет число **отправленных** строк
с `SELECT count()` из факта и бросает исключение при расхождении, а не отдаёт тихий успех.
Возвращает он тоже число из факта: раньше возвращалась длина входа, и главная проверка
проекта для ClickHouse не могла провалиться по построению.

**Edge cases**

- `init_schema` бьёт файл по `;` вручную: ClickHouse по HTTP исполняет **по одному**
  стейтменту. Перед разбиением срезаются `--`-комментарии, иначе `;` внутри комментария
  ломает разбиение
- Булевы приводятся к `int`: `int(v.is_remote)`, `None if v.salary_gross is None else
  int(...)` — сохраняется различие «нет данных» и «False»
- Таймаут HTTP-запроса 180 с — вставка батча на 2000 строк может быть долгой
- Догон уже поднятой БД идёт через `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` и
  `RENAME COLUMN`: `CREATE TABLE IF NOT EXISTS` существующую таблицу не меняет,
  а вставка `JSONEachRow` с неизвестной колонкой падает целиком

**Почему так.** Отказ от клиентской библиотеки — сознательный: HTTP-интерфейс ClickHouse
самодостаточен, а `requirements.txt` остаётся из двух строк.

---

## MS SQL Server

**Модуль.** `warehouse/mssql.py::MSSQLWarehouse`
**Драйвер.** `pymssql`, импортируется **лениво** внутри `_conn`
**Схема.** `sql/mssql/schema.sql`

**Модель.** Такая же звезда, как в PostgreSQL, но на T-SQL: `IDENTITY(1,1)` вместо
`SERIAL`, `NVARCHAR` вместо `TEXT` (обязательно — иначе кириллица превращается в `?`).

**Как грузит.** `DELETE` staging, затем `executemany` батчами по 1000, затем
`mssql.py::LOAD_SQL` с `MERGE` для каждого измерения:

```sql
MERGE core.cities AS t
USING (SELECT DISTINCT city_name FROM staging.stg_vacancies WHERE city_name IS NOT NULL) AS s
ON t.name = s.city_name
WHEN NOT MATCHED THEN INSERT(name) VALUES(s.city_name);
```

**Витрины.** Обычные views — всегда актуальны, обновлять нечего.

**Политика восстановления: целиком или никак — с 09.08.2026.** Соединение загрузки
открывается с `autocommit=False`, и очистка staging, вставка батчей и `LOAD_SQL` (внутри
которого `DELETE FROM core.vacancies`) идут одной транзакцией; `commit()` — только после
успешного чтения итогового `COUNT`. Обрыв на любом шаге -> выход из `with` -> `close()`,
который у pymssql делает неявный `ROLLBACK`, и в факте остаётся предыдущий срез. **Раньше
соединение было в autocommit**, и обрыв сразу после `DELETE` оставлял хранилище пустым —
расхождение с PostgreSQL видел только человек на дашборде.

*Компромисс:* на время транзакции `core.*` заблокированы для читателей — Power BI
и Metabase ждут либо видят прежний срез, тогда как в autocommit они видели бы пустую
таблицу. Полный срез ~90 тыс. строк заливается минуты, не часы, и «подождать» лучше,
чем «увидеть ноль».

**Edge cases**

- **`CREATE DATABASE` только из `master`** — T-SQL не позволяет создать БД, находясь
  в ней же. Отсюда отдельное подключение: `_conn(database="master")`
- **Батчи через `GO`** — `CREATE SCHEMA` и `CREATE VIEW` обязаны быть первыми
  в батче, поэтому файл схемы режется регуляркой `_GO` и исполняется по частям
- **`DELETE` вместо `TRUNCATE`** — `TRUNCATE` не работает при внешних ключах
- **Ленивый импорт `pymssql`** — драйвер опционален; его отсутствие не должно ломать
  импорт всего пакета, например в Airflow без mssql-таргета
- **Ключ вакансии — `NVARCHAR(200)`.** Был `NVARCHAR(80)` и на боевых данных ронял
  загрузку целиком: himalayas и arbeitnow неймспейсят id слагом, в срезе 732 id длиннее
  80 символов, самый длинный — 144 (200 взято с запасом). Резать id нельзя, это
  идентичность записи, а при выключенных `ANSI_WARNINGS` было бы хуже: усечение схлопнуло
  бы разные слаги в один id. В байтах ключ равен 400 при лимите индекса MS SQL 1700
- **Измерения с явной `COLLATE Latin1_General_100_CS_AS`.** Контейнер поднимается
  с дефолтной `SQL_Latin1_General_CP1_CI_AS`, то есть сравнением имён **без учёта
  регистра**, — и на одном входе измерения схлопывались не так, как в PG и CH: 806 имён
  работодателей и 90 локаций отличаются только регистром, поэтому `top_employers`
  и `city_stats` давали разное число строк и разные счётчики. Сторона выбрана в пользу
  двух движков из трёх (это же поведение у ленты родителя)
- **Guard-блок в начале схемы.** Ни ширину колонки в `PRIMARY KEY`, ни коллацию
  под `UNIQUE`-индексом простым `ALTER` не поменять, а `IF OBJECT_ID ... IS NULL CREATE
  TABLE` существующую таблицу не трогает — поэтому узкие или CI-коллационные таблицы
  **пересоздаются** (в порядке мост -> факт -> справочники, обратном FK). Данных это
  не теряет: staging и core перезаливаются целиком на каждом load
- **Города-агрегаторы режутся в домене** — `domain.py::_cap` обрезает city до
  `CITY_MAX = 200` code points (talanto шлёт списки стран до ~2400 симв.); одинаково для всех
  движков, поэтому числа сходятся. City-колонки MSSQL при этом `NVARCHAR(450)`, а не (200):
  эмодзи-флаги (🇦🇩 = суррогатные пары) раздувают 200 code points до ~400 UTF-16 юнитов
- **`BIGINT`, а не `INT`, в средних витрин** — перевод экзотических валют (UZS, KRW, VND)
  в рубли даёт большие числа, и переполнение уронило бы витрину целиком, а не одну строку
- **Медленнее остальных** — `executemany` вместо bulk-загрузки

**Почему так.** MS SQL добавлен, чтобы показать работу с Microsoft-стеком и T-SQL-специфику
(`MERGE`, `IDENTITY`, `NVARCHAR`), а также дать источник для Power BI.

---

## Сравнение

Одно и то же, тремя способами:

**Модель.** PG и MS SQL — звезда с мостом; ClickHouse — широкий факт с массивом.

**Набор витрин.** PG и MS SQL — пять; ClickHouse — три (нет `city_stats`
и `top_employers`, см. «Общая модель данных»).

**Обновление витрин.** PG — явный `REFRESH` на шаге load; ClickHouse — автоматически на
вставке; MS SQL — не требуется, views живые.

**Upsert измерений.** PG — `ON CONFLICT DO NOTHING`; MS SQL — `MERGE`; ClickHouse —
не применимо, измерений нет.

**Сравнение имён в измерениях.** PG — побайтовое (TEXT); ClickHouse — побайтовое (String);
MS SQL — побайтовое только благодаря явной `COLLATE ..._CS_AS`, дефолт контейнера
регистр игнорирует.

**Транспорт.** PG — `psycopg2`; MS SQL — `pymssql` (ленивый); ClickHouse — stdlib `urllib`.

**Очистка перед заливкой.** PG — `TRUNCATE`; MS SQL — `DELETE` (мешают FK);
ClickHouse — `TRUNCATE TABLE IF EXISTS`.

**Политика восстановления.** PG и MS SQL — одна транзакция, «выполнено целиком или
не выполнено вовсе». ClickHouse атомарным быть не может (нет транзакции на несколько
запросов), поэтому там частичный результат ловится сверкой отправленного с фактом.
Повтор безопасен во всех трёх: загрузка — полная замена факта.

**Округление агрегатов.** Правило общее — `round`, а не усечение (`CAST(AVG(...) AS INT)`
в T-SQL усекал дробную часть, и соседние карточки «сравнение движков» расходились
с PostgreSQL на единицу). PG и MS SQL совпадают доказуемо: `to_rub` возвращает **целое**
число рублей, поэтому среднее — дробь со знаменателем N <= числа записей, и ближайшее
не-ничейное значение отстоит от .5 не меньше чем на 1/(2N), а точные ничьи оба движка
округляют одинаково. **Единственное признанное расхождение — ClickHouse:** `round()`
над `Float64` там банковское (`round(0.5) = 0`), поэтому потребитель CH-витрины обязан
писать `floor(avgMerge(avg_min_rub) + 0.5)`. Расхождение неустранимо в самой схеме —
округление живёт в запросе потребителя.

## Проверка

```bash
python -m etl -t postgres all
python -m etl -t clickhouse all
python -m etl -t mssql all
python -m pytest tests/test_sql_schema.py -q   # что стережём в DDL, без БД
python -m pytest -m integration     # ВНИМАНИЕ: TRUNCATE'ит БД
```

Ожидаемо: `[etl] [<backend>] schema ready`, затем `[etl] [<backend>] loaded: N` и общая
строка `[etl] verify: prepare=N, в факте {…}`, где N одинаково для всех трёх движков.
Расхождение больше не требует внимательного читателя: `Pipeline._verify` роняет прогон
и называет отставший движок.
