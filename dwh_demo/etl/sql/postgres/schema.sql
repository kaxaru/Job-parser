-- Postgres DWH: staging (типизированный приём) -> core (звезда) -> mart (витрины).
-- Идемпотентна. Transform делается в Python (etl/domain.py), поэтому сырого
-- JSONB-слоя здесь нет — записи прилетают уже типизированными в staging.stg_*.

-- ────────────────────────── STAGING ──────────────────────────
CREATE SCHEMA IF NOT EXISTS staging;

-- id — TEXT: основной проект неймспейсит id по источникам (hirify_/talanto_<uuid>).
CREATE TABLE IF NOT EXISTS staging.stg_vacancies (
    id               TEXT PRIMARY KEY,
    source           TEXT,
    name             TEXT NOT NULL,
    city_name        TEXT,
    employer_name    TEXT,
    salary_min       NUMERIC,
    salary_max       NUMERIC,
    salary_currency  TEXT,
    salary_gross     BOOLEAN,
    experience       TEXT,
    schedule         TEXT,
    is_remote        BOOLEAN,
    url              TEXT,
    query            TEXT
);

CREATE TABLE IF NOT EXISTS staging.stg_skills (
    vacancy_id  TEXT NOT NULL,
    skill       TEXT NOT NULL
);

-- ─────────────────────────── CORE ────────────────────────────
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.cities    (id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS core.employers (id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS core.skills    (id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL);

CREATE TABLE IF NOT EXISTS core.vacancies (
    id               TEXT PRIMARY KEY,
    source           TEXT,
    name             TEXT NOT NULL,
    city_id          INT REFERENCES core.cities(id),
    employer_id      INT REFERENCES core.employers(id),
    salary_min       NUMERIC,
    salary_max       NUMERIC,
    salary_currency  TEXT,
    salary_gross     BOOLEAN,
    experience       TEXT,
    schedule         TEXT,
    is_remote        BOOLEAN,
    url              TEXT,
    query            TEXT,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vac_city     ON core.vacancies(city_id);
CREATE INDEX IF NOT EXISTS ix_vac_employer ON core.vacancies(employer_id);
CREATE INDEX IF NOT EXISTS ix_vac_remote   ON core.vacancies(is_remote);
CREATE INDEX IF NOT EXISTS ix_vac_source   ON core.vacancies(source);

CREATE TABLE IF NOT EXISTS core.vacancy_skills (
    vacancy_id  TEXT REFERENCES core.vacancies(id) ON DELETE CASCADE,
    skill_id    INT REFERENCES core.skills(id),
    PRIMARY KEY (vacancy_id, skill_id)
);

-- ─────────────────────────── MART ────────────────────────────
CREATE SCHEMA IF NOT EXISTS mart;

CREATE MATERIALIZED VIEW IF NOT EXISTS mart.city_stats AS
SELECT c.name AS city, count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary,
       round(avg(v.salary_min)) AS avg_salary_min,
       round(avg(v.salary_max)) AS avg_salary_max,
       round(100.0 * count(*) FILTER (WHERE v.is_remote) / count(*), 1) AS remote_share_pct
FROM core.vacancies v JOIN core.cities c ON c.id = v.city_id
GROUP BY c.name;

CREATE MATERIALIZED VIEW IF NOT EXISTS mart.skill_demand AS
SELECT s.name AS skill, count(*) AS vacancies
FROM core.vacancy_skills vs JOIN core.skills s ON s.id = vs.skill_id
GROUP BY s.name;

CREATE MATERIALIZED VIEW IF NOT EXISTS mart.salary_by_experience AS
SELECT coalesce(v.experience, 'не указан') AS experience, count(*) AS vacancies,
       round(avg(v.salary_min)) AS avg_salary_min, round(avg(v.salary_max)) AS avg_salary_max
FROM core.vacancies v
GROUP BY coalesce(v.experience, 'не указан');

CREATE MATERIALIZED VIEW IF NOT EXISTS mart.top_employers AS
SELECT e.name AS employer, count(*) AS vacancies
FROM core.vacancies v JOIN core.employers e ON e.id = v.employer_id
GROUP BY e.name;

-- витрина «портал-источник»: объём, доля с зарплатой, медиана вилки, доля remote
CREATE MATERIALIZED VIEW IF NOT EXISTS mart.source_stats AS
SELECT coalesce(v.source, 'hh') AS source, count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY v.salary_min)) AS median_salary_min,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY v.salary_max)) AS median_salary_max,
       round(100.0 * count(*) FILTER (WHERE v.is_remote) / count(*), 1) AS remote_share_pct
FROM core.vacancies v
GROUP BY coalesce(v.source, 'hh');
