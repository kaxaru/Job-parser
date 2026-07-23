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
    salary_currency Nullable(String),
    salary_gross    Nullable(UInt8),
    experience      Nullable(String),
    schedule        Nullable(String),
    is_remote       UInt8,
    url             Nullable(String),
    query           Nullable(String),
    skills          Array(String)
)
ENGINE = MergeTree
ORDER BY id;

-- витрина «спрос на навыки» (countState/countMerge + ARRAY JOIN)
CREATE TABLE IF NOT EXISTS hh.skill_demand
(
    skill     String,
    vacancies AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
ORDER BY skill;

CREATE MATERIALIZED VIEW IF NOT EXISTS hh.skill_demand_mv
TO hh.skill_demand
AS
SELECT skill, countState() AS vacancies
FROM hh.vacancies
ARRAY JOIN skills AS skill
GROUP BY skill;

-- витрина «зарплата по опыту» (avgState/avgMerge; алиас exp_bucket != колонке experience)
CREATE TABLE IF NOT EXISTS hh.salary_by_exp
(
    exp_bucket String,
    avg_min    AggregateFunction(avg, Nullable(Float64)),
    avg_max    AggregateFunction(avg, Nullable(Float64)),
    vacancies  AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
ORDER BY exp_bucket;

CREATE MATERIALIZED VIEW IF NOT EXISTS hh.salary_by_exp_mv
TO hh.salary_by_exp
AS
SELECT
    coalesce(experience, 'не указан') AS exp_bucket,
    avgState(salary_min)              AS avg_min,
    avgState(salary_max)              AS avg_max,
    countState()                      AS vacancies
FROM hh.vacancies
GROUP BY exp_bucket;

-- витрина «портал-источник»: объём, средняя вилка, доля remote (avg/count/sum-State)
CREATE TABLE IF NOT EXISTS hh.source_stats
(
    source     String,
    vacancies  AggregateFunction(count),
    avg_min    AggregateFunction(avg, Nullable(Float64)),
    avg_max    AggregateFunction(avg, Nullable(Float64)),
    remote     AggregateFunction(sum, UInt8)
)
ENGINE = AggregatingMergeTree
ORDER BY source;

CREATE MATERIALIZED VIEW IF NOT EXISTS hh.source_stats_mv
TO hh.source_stats
AS
SELECT
    source,
    countState()          AS vacancies,
    avgState(salary_min)  AS avg_min,
    avgState(salary_max)  AS avg_max,
    sumState(is_remote)   AS remote
FROM hh.vacancies
GROUP BY source;
