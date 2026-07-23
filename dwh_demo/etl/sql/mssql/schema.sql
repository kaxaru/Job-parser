-- MS SQL (T-SQL) DWH: staging -> core (звезда) -> mart (views). Идемпотентна.
-- Особенности T-SQL: IDENTITY, NVARCHAR (кириллица!), IF OBJECT_ID, GO-батчи,
-- CREATE OR ALTER VIEW. Батчи разделяются GO (адаптер бьёт файл по GO).

IF SCHEMA_ID('staging') IS NULL EXEC('CREATE SCHEMA staging');
GO
IF SCHEMA_ID('core') IS NULL EXEC('CREATE SCHEMA core');
GO
IF SCHEMA_ID('mart') IS NULL EXEC('CREATE SCHEMA mart');
GO

-- ───────── STAGING ─────────
-- id — NVARCHAR: основной проект неймспейсит id по источникам (hirify_/talanto_<uuid>).
IF OBJECT_ID('staging.stg_vacancies') IS NULL
CREATE TABLE staging.stg_vacancies (
    id              NVARCHAR(80)  PRIMARY KEY,
    source          NVARCHAR(20),
    name            NVARCHAR(500) NOT NULL,
    -- city_name NVARCHAR(450): домен режет city до 200 code points, но эмодзи-флаги
    -- (🇦🇩 = 2 суррогатные пары) раздувают строку до ~400 UTF-16 юнитов; 450 с запасом.
    city_name       NVARCHAR(450),
    employer_name   NVARCHAR(400),
    salary_min      DECIMAL(18,2),
    salary_max      DECIMAL(18,2),
    salary_currency NVARCHAR(10),
    salary_gross    BIT,
    experience      NVARCHAR(50),
    schedule        NVARCHAR(50),
    is_remote       BIT,
    url             NVARCHAR(500),
    query           NVARCHAR(200)
);
GO
IF OBJECT_ID('staging.stg_skills') IS NULL
CREATE TABLE staging.stg_skills (
    vacancy_id NVARCHAR(80)  NOT NULL,
    skill      NVARCHAR(100) NOT NULL
);
GO

-- ───────── CORE (звезда) ─────────
IF OBJECT_ID('core.cities') IS NULL
CREATE TABLE core.cities (id INT IDENTITY(1,1) PRIMARY KEY, name NVARCHAR(450) NOT NULL UNIQUE);
GO
IF OBJECT_ID('core.employers') IS NULL
CREATE TABLE core.employers (id INT IDENTITY(1,1) PRIMARY KEY, name NVARCHAR(400) NOT NULL UNIQUE);
GO
IF OBJECT_ID('core.skills') IS NULL
CREATE TABLE core.skills (id INT IDENTITY(1,1) PRIMARY KEY, name NVARCHAR(100) NOT NULL UNIQUE);
GO
IF OBJECT_ID('core.vacancies') IS NULL
CREATE TABLE core.vacancies (
    id              NVARCHAR(80) PRIMARY KEY,
    source          NVARCHAR(20),
    name            NVARCHAR(500) NOT NULL,
    city_id         INT NULL REFERENCES core.cities(id),
    employer_id     INT NULL REFERENCES core.employers(id),
    salary_min      DECIMAL(18,2),
    salary_max      DECIMAL(18,2),
    salary_currency NVARCHAR(10),
    salary_gross    BIT,
    experience      NVARCHAR(50),
    schedule        NVARCHAR(50),
    is_remote       BIT,
    url             NVARCHAR(500),
    query           NVARCHAR(200),
    loaded_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
IF OBJECT_ID('core.vacancy_skills') IS NULL
CREATE TABLE core.vacancy_skills (
    vacancy_id NVARCHAR(80) NOT NULL REFERENCES core.vacancies(id),
    skill_id   INT    NOT NULL REFERENCES core.skills(id),
    PRIMARY KEY (vacancy_id, skill_id)
);
GO

-- ───────── MART (views; в T-SQL materialized = indexed view с ограничениями, для демо обычные) ─────────
CREATE OR ALTER VIEW mart.skill_demand AS
SELECT s.name AS skill, COUNT(*) AS vacancies
FROM core.vacancy_skills vs JOIN core.skills s ON s.id = vs.skill_id
GROUP BY s.name;
GO
CREATE OR ALTER VIEW mart.salary_by_experience AS
SELECT COALESCE(v.experience, N'не указан') AS experience,
       COUNT(*)                              AS vacancies,
       CAST(AVG(v.salary_min) AS INT)        AS avg_salary_min,
       CAST(AVG(v.salary_max) AS INT)        AS avg_salary_max
FROM core.vacancies v
GROUP BY COALESCE(v.experience, N'не указан');
GO
CREATE OR ALTER VIEW mart.top_employers AS
SELECT e.name AS employer, COUNT(*) AS vacancies
FROM core.vacancies v JOIN core.employers e ON e.id = v.employer_id
GROUP BY e.name;
GO
CREATE OR ALTER VIEW mart.city_stats AS
SELECT c.name AS city,
       COUNT(*) AS vacancies,
       SUM(CASE WHEN v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL THEN 1 ELSE 0 END) AS with_salary,
       CAST(AVG(v.salary_max) AS INT) AS avg_salary_max,
       CAST(100.0 * SUM(CASE WHEN v.is_remote = 1 THEN 1 ELSE 0 END) / COUNT(*) AS DECIMAL(5,1)) AS remote_share_pct
FROM core.vacancies v JOIN core.cities c ON c.id = v.city_id
GROUP BY c.name;
GO
CREATE OR ALTER VIEW mart.source_stats AS
SELECT COALESCE(v.source, 'hh') AS source,
       COUNT(*) AS vacancies,
       SUM(CASE WHEN v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL THEN 1 ELSE 0 END) AS with_salary,
       CAST(AVG(v.salary_min) AS INT) AS avg_salary_min,
       CAST(AVG(v.salary_max) AS INT) AS avg_salary_max,
       CAST(100.0 * SUM(CASE WHEN v.is_remote = 1 THEN 1 ELSE 0 END) / COUNT(*) AS DECIMAL(5,1)) AS remote_share_pct
FROM core.vacancies v
GROUP BY COALESCE(v.source, 'hh');
GO
