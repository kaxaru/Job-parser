-- MS SQL (T-SQL) DWH: staging -> core (звезда) -> mart (views). Идемпотентна.
-- Особенности T-SQL: IDENTITY, NVARCHAR (кириллица!), IF OBJECT_ID, GO-батчи,
-- CREATE OR ALTER VIEW. Батчи разделяются GO (адаптер бьёт файл по GO).

IF SCHEMA_ID('staging') IS NULL EXEC('CREATE SCHEMA staging');
GO
IF SCHEMA_ID('core') IS NULL EXEC('CREATE SCHEMA core');
GO
IF SCHEMA_ID('mart') IS NULL EXEC('CREATE SCHEMA mart');
GO

-- ───────── догон уже поднятой БД: ширина ключа вакансии (09.08.2026) ─────────
-- Ключ был NVARCHAR(80) и на боевых данных ронял загрузку целиком: himalayas и arbeitnow
-- неймспейсят id слагом, в срезе 732 id длиннее 80 символов, самый длинный — 144. SQL Server
-- отвечает на такой INSERT ошибкой 8152, а при выключенных ANSI_WARNINGS было бы хуже:
-- усечение схлопнуло бы разные слаги в один id и уронило PRIMARY KEY. Резать id нельзя —
-- это идентичность записи. `IF OBJECT_ID ... IS NULL CREATE TABLE` ширину существующей
-- колонки не меняет, а расширить её через ALTER нельзя, пока она в PRIMARY KEY и во внешнем
-- ключе, поэтому узкие таблицы ПЕРЕСОЗДАЮТСЯ. Данных это не теряет: и staging, и core
-- перезаливаются целиком на каждом load (`mssql.py::LOAD_SQL`).
-- COL_LENGTH отдаёт длину В БАЙТАХ: NVARCHAR(80) = 160, NVARCHAR(200) = 400.
-- Для отсутствующей таблицы COL_LENGTH = NULL, сравнение даёт UNKNOWN и блок не срабатывает.
IF COL_LENGTH('staging.stg_vacancies', 'id') < 400 DROP TABLE staging.stg_vacancies;
GO
IF COL_LENGTH('staging.stg_skills', 'vacancy_id') < 400 DROP TABLE staging.stg_skills;
GO
IF COL_LENGTH('core.vacancy_skills', 'vacancy_id') < 400 DROP TABLE core.vacancy_skills;
GO
IF COL_LENGTH('core.vacancies', 'id') < 400
BEGIN
    -- сначала мост: у него FK на core.vacancies
    IF OBJECT_ID('core.vacancy_skills') IS NOT NULL DROP TABLE core.vacancy_skills;
    DROP TABLE core.vacancies;
END
GO

-- ───────── догон уже поднятой БД: коллация измерений (09.08.2026) ─────────
-- Контейнер mssql поднимается с дефолтной SQL_Latin1_General_CP1_CI_AS, то есть сравнение
-- имён БЕЗ УЧЁТА РЕГИСТРА. PostgreSQL (TEXT UNIQUE) и ClickHouse (String) сравнивают
-- побайтово, и на одном входе измерения схлопывались по-разному: 806 имён работодателей
-- и 90 локаций отличаются ТОЛЬКО регистром, поэтому mart.top_employers и mart.city_stats
-- давали разное число строк и разные счётчики в PG и MS SQL. Сторона выбрана в пользу
-- PG/CH (два движка из трёх, и это же поведение у ленты родителя): на колонках измерений
-- и на их источниках в staging стоит явный COLLATE Latin1_General_100_CS_AS. Нормализация
-- регистра, если понадобится, — в ЕДИНОМ transform `etl/domain.py`, а не в правилах БД.
-- Коллация колонки меняется только через ALTER с пересозданием UNIQUE-индекса, поэтому
-- узкое место закрывается так же, как ширина ключа выше: таблицы ПЕРЕСОЗДАЮТСЯ. Данных
-- это не теряет — staging и core перезаливаются целиком на каждом load, а справочники
-- восстанавливает MERGE из staging (`mssql.py::LOAD_SQL`).
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('staging.stg_vacancies')
             AND name IN ('city_name', 'employer_name')
             AND collation_name <> 'Latin1_General_100_CS_AS')
    DROP TABLE staging.stg_vacancies;
GO
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('staging.stg_skills')
             AND name = 'skill' AND collation_name <> 'Latin1_General_100_CS_AS')
    DROP TABLE staging.stg_skills;
GO
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id IN (OBJECT_ID('core.cities'), OBJECT_ID('core.employers'),
                               OBJECT_ID('core.skills'))
             AND name = 'name' AND collation_name <> 'Latin1_General_100_CS_AS')
BEGIN
    -- порядок обязателен: мост -> факт -> справочники (FK держат обратный)
    IF OBJECT_ID('core.vacancy_skills') IS NOT NULL DROP TABLE core.vacancy_skills;
    IF OBJECT_ID('core.vacancies')      IS NOT NULL DROP TABLE core.vacancies;
    IF OBJECT_ID('core.cities')         IS NOT NULL DROP TABLE core.cities;
    IF OBJECT_ID('core.employers')      IS NOT NULL DROP TABLE core.employers;
    IF OBJECT_ID('core.skills')         IS NOT NULL DROP TABLE core.skills;
END
GO

-- ───────── STAGING ─────────
-- id — NVARCHAR(200): основной проект неймспейсит id по источникам (hirify_733072,
-- talanto_<uuid>), а himalayas/arbeitnow — слагом вакансии; замер по кешу даёт максимум
-- 144 символа, 200 взято с запасом. Ключ в байтах: 200 * 2 = 400 при лимите индекса
-- MS SQL в 1700 байт (у составного PK core.vacancy_skills — 404 с учётом INT).
IF OBJECT_ID('staging.stg_vacancies') IS NULL
CREATE TABLE staging.stg_vacancies (
    id              NVARCHAR(200) PRIMARY KEY,
    source          NVARCHAR(20),
    name            NVARCHAR(500) NOT NULL,
    -- city_name NVARCHAR(450): домен режет city до 200 code points, но эмодзи-флаги
    -- (🇦🇩 = 2 суррогатные пары) раздувают строку до ~400 UTF-16 юнитов; 450 с запасом.
    -- Это подпись ЛОКАЦИИ портала (город ИЛИ страна); сентинел родителя `Remote`
    -- в измерение не попадает (`domain.py::_location`).
    -- COLLATE ..._CS_AS — чтобы MERGE/JOIN на core.cities схлопывали имена так же
    -- побайтово, как PostgreSQL и ClickHouse (обоснование — в блоке догона выше).
    city_name       NVARCHAR(450) COLLATE Latin1_General_100_CS_AS,
    employer_name   NVARCHAR(400) COLLATE Latin1_General_100_CS_AS,
    salary_min      DECIMAL(18,2),
    salary_max      DECIMAL(18,2),
    -- вилка, приведённая к рублям в домене (`domain.py::Vacancy.from_raw` -> `rates.to_rub`).
    -- Усреднять можно ТОЛЬКО эти колонки: salary_min/salary_max лежат каждая в своей валюте.
    salary_min_rub  DECIMAL(18,2),
    salary_max_rub  DECIMAL(18,2),
    salary_currency NVARCHAR(10),
    salary_gross    BIT,
    experience      NVARCHAR(50),
    schedule        NVARCHAR(50),
    is_remote       BIT,
    -- словесный маркер удалёнки в тексте — ОТДЕЛЬНОЕ понятие, не формат работы
    remote_mentioned BIT,
    url             NVARCHAR(500)
);
GO
IF OBJECT_ID('staging.stg_skills') IS NULL
CREATE TABLE staging.stg_skills (
    vacancy_id NVARCHAR(200) NOT NULL,
    skill      NVARCHAR(100) COLLATE Latin1_General_100_CS_AS NOT NULL
);
GO

-- ───────── CORE (звезда) ─────────
-- COLLATE ..._CS_AS на всех трёх измерениях: схлопывание имён обязано быть побайтовым,
-- как в PostgreSQL и ClickHouse (обоснование и догон поднятой БД — в блоке выше).
IF OBJECT_ID('core.cities') IS NULL
CREATE TABLE core.cities (id INT IDENTITY(1,1) PRIMARY KEY,
    name NVARCHAR(450) COLLATE Latin1_General_100_CS_AS NOT NULL UNIQUE);
GO
IF OBJECT_ID('core.employers') IS NULL
CREATE TABLE core.employers (id INT IDENTITY(1,1) PRIMARY KEY,
    name NVARCHAR(400) COLLATE Latin1_General_100_CS_AS NOT NULL UNIQUE);
GO
IF OBJECT_ID('core.skills') IS NULL
CREATE TABLE core.skills (id INT IDENTITY(1,1) PRIMARY KEY,
    name NVARCHAR(100) COLLATE Latin1_General_100_CS_AS NOT NULL UNIQUE);
GO
IF OBJECT_ID('core.vacancies') IS NULL
CREATE TABLE core.vacancies (
    id              NVARCHAR(200) PRIMARY KEY,
    source          NVARCHAR(20),
    name            NVARCHAR(500) NOT NULL,
    city_id         INT NULL REFERENCES core.cities(id),
    employer_id     INT NULL REFERENCES core.employers(id),
    salary_min      DECIMAL(18,2),
    salary_max      DECIMAL(18,2),
    salary_min_rub  DECIMAL(18,2),      -- см. комментарий в staging.stg_vacancies
    salary_max_rub  DECIMAL(18,2),
    salary_currency NVARCHAR(10),
    salary_gross    BIT,
    experience      NVARCHAR(50),
    schedule        NVARCHAR(50),
    is_remote       BIT,
    remote_mentioned BIT,
    url             NVARCHAR(500),
    loaded_at       DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
IF OBJECT_ID('core.vacancy_skills') IS NULL
CREATE TABLE core.vacancy_skills (
    vacancy_id NVARCHAR(200) NOT NULL REFERENCES core.vacancies(id),
    skill_id   INT    NOT NULL REFERENCES core.skills(id),
    PRIMARY KEY (vacancy_id, skill_id)
);
GO

-- ───────── догон уже поднятой БД: рублёвая вилка + remote_mentioned (09.08.2026) ─────────
-- Проверка на каждую колонку отдельно: том мог остаться от промежуточного состояния схемы.
IF COL_LENGTH('staging.stg_vacancies', 'salary_min_rub') IS NULL
    ALTER TABLE staging.stg_vacancies ADD salary_min_rub DECIMAL(18,2) NULL;
IF COL_LENGTH('staging.stg_vacancies', 'salary_max_rub') IS NULL
    ALTER TABLE staging.stg_vacancies ADD salary_max_rub DECIMAL(18,2) NULL;
IF COL_LENGTH('staging.stg_vacancies', 'remote_mentioned') IS NULL
    ALTER TABLE staging.stg_vacancies ADD remote_mentioned BIT NULL;
GO
IF COL_LENGTH('core.vacancies', 'salary_min_rub') IS NULL
    ALTER TABLE core.vacancies ADD salary_min_rub DECIMAL(18,2) NULL;
IF COL_LENGTH('core.vacancies', 'salary_max_rub') IS NULL
    ALTER TABLE core.vacancies ADD salary_max_rub DECIMAL(18,2) NULL;
IF COL_LENGTH('core.vacancies', 'remote_mentioned') IS NULL
    ALTER TABLE core.vacancies ADD remote_mentioned BIT NULL;
GO

-- `query` (из какого поискового запроса пришла вакансия) УДАЛЁН 09.08.2026: родитель это
-- поле больше не пишет — перепись ключей по всем 109 597 записям кеша даёт 19 имён, `_query`
-- среди них нет. Колонка была гарантированно NULL и обещала срез, которого не существует;
-- `IF OBJECT_ID ... IS NULL CREATE TABLE` её бы не убрал никогда.
IF COL_LENGTH('staging.stg_vacancies', 'query') IS NOT NULL
    ALTER TABLE staging.stg_vacancies DROP COLUMN query;
IF COL_LENGTH('core.vacancies', 'query') IS NOT NULL
    ALTER TABLE core.vacancies DROP COLUMN query;
GO

-- ───────── MART (views; в T-SQL materialized = indexed view с ограничениями, для демо обычные) ─────────
-- Спецификация метрик — общая на три движка, полностью выписана в sql/postgres/schema.sql:
-- считаем по `salary_*_rub`, имя метрики несёт единицу (`_rub`), обе средние берутся по
-- ОДНОМУ множеству строк (обе рублёвые границы известны; их число — `with_salary_rub`),
-- округление — ROUND, а НЕ усечение. `CAST(AVG(...) AS INT)` здесь усекал дробную часть,
-- и соседние карточки «сравнение движков» расходились с PostgreSQL на единицу.
-- BIGINT, а не INT: перевод экзотических валют (UZS, KRW, VND) в рубли даёт большие числа,
-- и переполнение INT уронило бы всю витрину целиком, а не одну строку.
CREATE OR ALTER VIEW mart.skill_demand AS
SELECT s.name AS skill, COUNT(*) AS vacancies
FROM core.vacancy_skills vs JOIN core.skills s ON s.id = vs.skill_id
GROUP BY s.name;
GO
-- Бакет «не указан» ловит и NULL, и ПУСТУЮ СТРОКУ (`NULLIF` перед `COALESCE`): голый
-- COALESCE не срабатывал никогда, и 20 650 вакансий уходили в бакет с пустой подписью.
CREATE OR ALTER VIEW mart.salary_by_experience AS
SELECT COALESCE(NULLIF(v.experience, N''), N'не указан') AS experience,
       COUNT(*) AS vacancies,
       SUM(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL THEN 1 ELSE 0 END) AS with_salary_rub,
       CAST(ROUND(AVG(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL
                           THEN v.salary_min_rub END), 0) AS BIGINT) AS avg_salary_min_rub,
       CAST(ROUND(AVG(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL
                           THEN v.salary_max_rub END), 0) AS BIGINT) AS avg_salary_max_rub
FROM core.vacancies v
GROUP BY COALESCE(NULLIF(v.experience, N''), N'не указан');
GO
CREATE OR ALTER VIEW mart.top_employers AS
SELECT e.name AS employer, COUNT(*) AS vacancies
FROM core.vacancies v JOIN core.employers e ON e.id = v.employer_id
GROUP BY e.name;
GO
-- Локация НЕИЗВЕСТНА (city_id IS NULL) — бакет, а не выпадение из среза: факт грузится
-- LEFT JOIN'ом на измерения, и INNER JOIN здесь молча терял 1 301 вакансию — сумма
-- `vacancies` по city_stats не сходилась с mart.source_stats на одном дашборде.
-- Подпись бакета дословно та же, что в PostgreSQL (`domain.py::NO_CITY_LABEL`).
CREATE OR ALTER VIEW mart.city_stats AS
SELECT COALESCE(c.name, N'не указана') AS city,
       COUNT(*) AS vacancies,
       SUM(CASE WHEN v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL THEN 1 ELSE 0 END) AS with_salary,
       SUM(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL THEN 1 ELSE 0 END) AS with_salary_rub,
       CAST(ROUND(AVG(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL
                           THEN v.salary_min_rub END), 0) AS BIGINT) AS avg_salary_min_rub,
       CAST(ROUND(AVG(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL
                           THEN v.salary_max_rub END), 0) AS BIGINT) AS avg_salary_max_rub,
       CAST(100.0 * SUM(CASE WHEN v.is_remote = 1 THEN 1 ELSE 0 END) / COUNT(*) AS DECIMAL(5,1)) AS remote_share_pct
FROM core.vacancies v LEFT JOIN core.cities c ON c.id = v.city_id
GROUP BY COALESCE(c.name, N'не указана');
GO
-- Метрика — СРЕДНЕЕ, как в PostgreSQL и ClickHouse: до 09.08.2026 одноимённая витрина
-- в PG отдавала медиану, а здесь среднее, и сравнивать движки по ней было нельзя.
CREATE OR ALTER VIEW mart.source_stats AS
SELECT COALESCE(v.source, 'hh') AS source,
       COUNT(*) AS vacancies,
       SUM(CASE WHEN v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL THEN 1 ELSE 0 END) AS with_salary,
       SUM(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL THEN 1 ELSE 0 END) AS with_salary_rub,
       CAST(ROUND(AVG(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL
                           THEN v.salary_min_rub END), 0) AS BIGINT) AS avg_salary_min_rub,
       CAST(ROUND(AVG(CASE WHEN v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL
                           THEN v.salary_max_rub END), 0) AS BIGINT) AS avg_salary_max_rub,
       CAST(100.0 * SUM(CASE WHEN v.is_remote = 1 THEN 1 ELSE 0 END) / COUNT(*) AS DECIMAL(5,1)) AS remote_share_pct
FROM core.vacancies v
GROUP BY COALESCE(v.source, 'hh');
GO
