-- ClickHouse DWH: широкий факт + инкрементальные витрины (AggregatingMergeTree).
-- В отличие от Postgres — без REFRESH: MV доагрегируют на каждой вставке.

CREATE DATABASE IF NOT EXISTS hh;

-- id — String: основной проект неймспейсит id по источникам (hirify_/talanto_<uuid>).
CREATE TABLE IF NOT EXISTS hh.vacancies
(
    id              String,
    source          String,
    name            String,
    city            Nullable(String),
    employer        Nullable(String),
    salary_min      Nullable(Float64),
    salary_max      Nullable(Float64),
    -- вилка, приведённая к рублям в домене (`domain.py::Vacancy.from_raw` -> `rates.to_rub`).
    -- Усреднять можно ТОЛЬКО эти колонки: salary_min/salary_max лежат каждая в своей валюте
    -- (в срезе 45 кодов). Неизвестный курс/пустая валюта -> NULL, вакансия выпадает
    -- из зарплатного среза, а не считается нулём.
    salary_min_rub  Nullable(Float64),
    salary_max_rub  Nullable(Float64),
    salary_currency Nullable(String),
    salary_gross    Nullable(UInt8),
    experience      Nullable(String),
    schedule        Nullable(String),
    is_remote       UInt8,
    -- словесный маркер удалёнки в тексте — ОТДЕЛЬНОЕ понятие, не формат работы
    remote_mentioned UInt8,
    url             Nullable(String),
    query           Nullable(String),
    skills          Array(String)
)
ENGINE = MergeTree
ORDER BY id;

-- Догон уже поднятой БД (09.08.2026): CREATE TABLE IF NOT EXISTS существующую таблицу
-- не меняет, а вставка JSONEachRow с неизвестной колонкой падает целиком.
ALTER TABLE hh.vacancies ADD COLUMN IF NOT EXISTS salary_min_rub   Nullable(Float64);
ALTER TABLE hh.vacancies ADD COLUMN IF NOT EXISTS salary_max_rub   Nullable(Float64);
ALTER TABLE hh.vacancies ADD COLUMN IF NOT EXISTS remote_mentioned UInt8;

-- ── единица измерения и округление (общая спецификация трёх схем) ──
-- Полностью выписана в sql/postgres/schema.sql. Здесь важны два отличия ClickHouse:
-- 1. Витрина хранит СОСТОЯНИЯ (AggregateFunction), а не числа: доля remote и любое
--    округление считаются потребителем на avgMerge/sumMerge/countMerge.
-- 2. `round()` над Float64 в ClickHouse — БАНКОВСКОЕ округление (round(0.5) = 0), тогда как
--    PostgreSQL и MS SQL округляют ничьи от нуля. На целых рублях ничья (avg = k + 0.5)
--    достижима в любом чётном бакете, поэтому потребитель CH-витрины обязан писать
--    `floor(avgMerge(avg_min_rub) + 0.5)`, а не `round(avgMerge(avg_min_rub))` —
--    иначе три движка разойдутся на единицу на одном и том же входе.
--    Расхождение неустранимо в самой схеме: округление живёт в запросе потребителя.

-- витрина «спрос на навыки» (countState/countMerge + ARRAY JOIN)
CREATE TABLE IF NOT EXISTS hh.skill_demand
(
    skill     String,
    vacancies AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
ORDER BY skill;

-- витрина «зарплата по опыту» (avgState/avgMerge; алиас exp_bucket != колонке experience).
-- Порядок колонок = порядок, в который приходит уже поднятая БД после ALTER ниже.
CREATE TABLE IF NOT EXISTS hh.salary_by_exp
(
    exp_bucket      String,
    avg_min_rub     AggregateFunction(avg, Nullable(Float64)),
    avg_max_rub     AggregateFunction(avg, Nullable(Float64)),
    vacancies       AggregateFunction(count),
    with_salary_rub AggregateFunction(sum, UInt8)
)
ENGINE = AggregatingMergeTree
ORDER BY exp_bucket;

-- витрина «портал-источник»: объём, средняя вилка в рублях, remote, покрытие вилкой
CREATE TABLE IF NOT EXISTS hh.source_stats
(
    source          String,
    vacancies       AggregateFunction(count),
    avg_min_rub     AggregateFunction(avg, Nullable(Float64)),
    avg_max_rub     AggregateFunction(avg, Nullable(Float64)),
    remote          AggregateFunction(sum, UInt8),
    with_salary_rub AggregateFunction(sum, UInt8)
)
ENGINE = AggregatingMergeTree
ORDER BY source;

-- Догон уже поднятой БД: имена зарплатных колонок сменились на `_rub` (метрика обязана
-- называть единицу), добавилось покрытие вилкой. RENAME сохраняет позицию колонки,
-- ADD дописывает в конец — поэтому CREATE выше перечисляет колонки в том же порядке,
-- в каком их получит мигрированная таблица.
ALTER TABLE hh.salary_by_exp RENAME COLUMN IF EXISTS avg_min TO avg_min_rub;
ALTER TABLE hh.salary_by_exp RENAME COLUMN IF EXISTS avg_max TO avg_max_rub;
ALTER TABLE hh.salary_by_exp ADD COLUMN IF NOT EXISTS with_salary_rub AggregateFunction(sum, UInt8);
ALTER TABLE hh.source_stats  RENAME COLUMN IF EXISTS avg_min TO avg_min_rub;
ALTER TABLE hh.source_stats  RENAME COLUMN IF EXISTS avg_max TO avg_max_rub;
ALTER TABLE hh.source_stats  ADD COLUMN IF NOT EXISTS with_salary_rub AggregateFunction(sum, UInt8);

-- MV ПЕРЕСОЗДАЮТСЯ (DROP + CREATE), а не `CREATE ... IF NOT EXISTS`: иначе правка
-- определения доезжает в MS SQL (`CREATE OR ALTER VIEW`) немедленно, а сюда — никогда,
-- и движки расходятся навсегда при зелёной проверке идемпотентности. Данные витрин
-- при этом не теряются: MV с `TO` — только правило вставки, таблица-приёмник живёт сама
-- (и всё равно перезаливается на каждом load, `clickhouse.py::_TRUNCATE`).

DROP VIEW IF EXISTS hh.skill_demand_mv;
CREATE MATERIALIZED VIEW hh.skill_demand_mv
TO hh.skill_demand
AS
SELECT skill, countState() AS vacancies
FROM hh.vacancies
ARRAY JOIN skills AS skill
GROUP BY skill;

-- Бакет «не указан» ловит и NULL, и ПУСТУЮ СТРОКУ (`nullIf` перед `coalesce`): голый
-- coalesce не срабатывал никогда, и 20 650 вакансий уходили в бакет с пустой подписью.
-- Обе средние — по ОДНОМУ множеству строк (известны обе рублёвые границы), его размер
-- виден в with_salary_rub; иначе серии графика «вилка» считаются по разным выборкам.
DROP VIEW IF EXISTS hh.salary_by_exp_mv;
CREATE MATERIALIZED VIEW hh.salary_by_exp_mv
TO hh.salary_by_exp
AS
SELECT
    coalesce(nullIf(experience, ''), 'не указан') AS exp_bucket,
    avgState(if(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub), salary_min_rub, NULL)) AS avg_min_rub,
    avgState(if(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub), salary_max_rub, NULL)) AS avg_max_rub,
    countState() AS vacancies,
    sumState(toUInt8(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub))) AS with_salary_rub
FROM hh.vacancies
GROUP BY exp_bucket;

-- Доля remote считается потребителем: 100 * sumMerge(remote) / countMerge(vacancies).
-- Хранить долю в AggregatingMergeTree нельзя — состояния складываются, проценты нет.
DROP VIEW IF EXISTS hh.source_stats_mv;
CREATE MATERIALIZED VIEW hh.source_stats_mv
TO hh.source_stats
AS
SELECT
    source,
    countState() AS vacancies,
    avgState(if(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub), salary_min_rub, NULL)) AS avg_min_rub,
    avgState(if(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub), salary_max_rub, NULL)) AS avg_max_rub,
    sumState(is_remote) AS remote,
    sumState(toUInt8(isNotNull(salary_min_rub) AND isNotNull(salary_max_rub))) AS with_salary_rub
FROM hh.vacancies
GROUP BY source;
