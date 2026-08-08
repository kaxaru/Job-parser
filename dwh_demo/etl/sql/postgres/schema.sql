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
    -- вилка, приведённая к рублям в домене (`domain.py::Vacancy.from_raw` -> `rates.to_rub`).
    -- Складывать и усреднять можно ТОЛЬКО эти колонки: `salary_min/salary_max` лежат
    -- каждая в своей валюте (в срезе 45 кодов, доллар встречается чаще рубля).
    -- Неизвестный курс/пустая валюта -> NULL: вакансия выпадает из зарплатного среза,
    -- а не считается нулём.
    salary_min_rub   NUMERIC,
    salary_max_rub   NUMERIC,
    salary_currency  TEXT,
    salary_gross     BOOLEAN,
    experience       TEXT,
    schedule         TEXT,
    is_remote        BOOLEAN,
    -- словесный маркер удалёнки в тексте — ОТДЕЛЬНОЕ понятие, не формат работы
    remote_mentioned BOOLEAN,
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
    salary_min_rub   NUMERIC,          -- см. комментарий в staging.stg_vacancies
    salary_max_rub   NUMERIC,
    salary_currency  TEXT,
    salary_gross     BOOLEAN,
    experience       TEXT,
    schedule         TEXT,
    is_remote        BOOLEAN,
    remote_mentioned BOOLEAN,
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

-- ─── догон уже поднятой БД (09.08.2026: рублёвая вилка + remote_mentioned) ───
-- `CREATE TABLE IF NOT EXISTS` существующую таблицу НЕ меняет, поэтому новые колонки
-- доезжают отдельным идемпотентным ALTER. Без него на живом томе load упал бы
-- на «column salary_min_rub does not exist».
ALTER TABLE staging.stg_vacancies ADD COLUMN IF NOT EXISTS salary_min_rub   NUMERIC;
ALTER TABLE staging.stg_vacancies ADD COLUMN IF NOT EXISTS salary_max_rub   NUMERIC;
ALTER TABLE staging.stg_vacancies ADD COLUMN IF NOT EXISTS remote_mentioned BOOLEAN;
ALTER TABLE core.vacancies        ADD COLUMN IF NOT EXISTS salary_min_rub   NUMERIC;
ALTER TABLE core.vacancies        ADD COLUMN IF NOT EXISTS salary_max_rub   NUMERIC;
ALTER TABLE core.vacancies        ADD COLUMN IF NOT EXISTS remote_mentioned BOOLEAN;

-- ─────────────────────────── MART ────────────────────────────
CREATE SCHEMA IF NOT EXISTS mart;

-- Витрины ПЕРЕСОЗДАЮТСЯ (DROP + CREATE), а не `CREATE ... IF NOT EXISTS`: иначе правка
-- определения доезжает в MS SQL (там `CREATE OR ALTER VIEW`) немедленно, а сюда — никогда,
-- и движки расходятся навсегда при зелёной проверке идемпотентности. `CREATE MATERIALIZED
-- VIEW` в PostgreSQL наполняет витрину сразу (WITH DATA по умолчанию), так что окна
-- с пустыми дашбордами между init и load не возникает.

-- ── единица измерения и округление (общая спецификация трёх схем) ──
-- 1. Все зарплатные метрики считаются по `salary_*_rub` и НАЗЫВАЮТСЯ с суффиксом `_rub`:
--    человек в Metabase должен видеть единицу, не открывая SQL.
-- 2. Обе средние берутся по ОДНОМУ множеству строк — где известны ОБЕ рублёвые границы
--    (`with_salary_rub` — его размер). Иначе серии графика «вилка» считаются по разным
--    выборкам: односторонних «от X» в срезе 8 948, «до Y» — 2 116, и верхний столбик
--    может оказаться ниже нижнего.
-- 3. `with_salary` — покрытие вилкой в ЛЮБОЙ валюте (метрика полноты данных),
--    а не знаменатель средних; путать их нельзя.
-- 4. Округление — `round` (ничьи от нуля), НЕ усечение. MS SQL считает то же самое
--    `CAST(ROUND(AVG(...), 0) AS BIGINT)`: `to_rub` возвращает ЦЕЛОЕ число рублей,
--    поэтому среднее — дробь со знаменателем N <= числа записей, ближайшее не-ничейное
--    значение отстоит от .5 не менее чем на 1/(2N) ~ 4,6e-6 — крупнее половины кванта
--    scale 6 у T-SQL AVG(DECIMAL), а точные ничьи оба движка округляют одинаково.
--    ClickHouse ОТЛИЧАЕТСЯ на точных ничьих: `round()` над Float64 там банковское
--    (round(0.5) = 0), поэтому потребитель CH-витрины обязан писать
--    `floor(avgMerge(avg_min_rub) + 0.5)` — см. комментарий в sql/clickhouse/schema.sql.

DROP MATERIALIZED VIEW IF EXISTS mart.city_stats;
CREATE MATERIALIZED VIEW mart.city_stats AS
SELECT c.name AS city, count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary,
       count(*) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL) AS with_salary_rub,
       round(avg(v.salary_min_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_min_rub,
       round(avg(v.salary_max_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_max_rub,
       round(100.0 * count(*) FILTER (WHERE v.is_remote) / count(*), 1) AS remote_share_pct
FROM core.vacancies v JOIN core.cities c ON c.id = v.city_id
GROUP BY c.name;

DROP MATERIALIZED VIEW IF EXISTS mart.skill_demand;
CREATE MATERIALIZED VIEW mart.skill_demand AS
SELECT s.name AS skill, count(*) AS vacancies
FROM core.vacancy_skills vs JOIN core.skills s ON s.id = vs.skill_id
GROUP BY s.name;

-- Бакет «не указан» ловит и NULL, и ПУСТУЮ СТРОКУ: `nullif(...,'')` перед `coalesce`.
-- Домен пустой код уже отдаёт как None (`domain.py::_label`), но витрина обязана быть
-- устойчивой к обоим представлениям — до 09.08.2026 голый `coalesce` не срабатывал
-- никогда, и 20 650 вакансий (19 %) уходили в бакет с ПУСТОЙ подписью.
DROP MATERIALIZED VIEW IF EXISTS mart.salary_by_experience;
CREATE MATERIALIZED VIEW mart.salary_by_experience AS
SELECT coalesce(nullif(v.experience, ''), 'не указан') AS experience,
       count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL) AS with_salary_rub,
       round(avg(v.salary_min_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_min_rub,
       round(avg(v.salary_max_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_max_rub
FROM core.vacancies v
GROUP BY coalesce(nullif(v.experience, ''), 'не указан');

DROP MATERIALIZED VIEW IF EXISTS mart.top_employers;
CREATE MATERIALIZED VIEW mart.top_employers AS
SELECT e.name AS employer, count(*) AS vacancies
FROM core.vacancies v JOIN core.employers e ON e.id = v.employer_id
GROUP BY e.name;

-- витрина «портал-источник»: объём, доля с зарплатой, средняя вилка в рублях, доля remote.
-- Метрика — СРЕДНЕЕ, а не медиана: одноимённая витрина в MS SQL и ClickHouse считает
-- среднее, а `percentile_cont` в T-SQL существует только как оконная функция. Одно имя
-- витрины обязано означать одно число во всех трёх движках.
DROP MATERIALIZED VIEW IF EXISTS mart.source_stats;
CREATE MATERIALIZED VIEW mart.source_stats AS
SELECT coalesce(v.source, 'hh') AS source, count(*) AS vacancies,
       count(*) FILTER (WHERE v.salary_min IS NOT NULL OR v.salary_max IS NOT NULL) AS with_salary,
       count(*) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL) AS with_salary_rub,
       round(avg(v.salary_min_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_min_rub,
       round(avg(v.salary_max_rub) FILTER (WHERE v.salary_min_rub IS NOT NULL AND v.salary_max_rub IS NOT NULL)) AS avg_salary_max_rub,
       round(100.0 * count(*) FILTER (WHERE v.is_remote) / count(*), 1) AS remote_share_pct
FROM core.vacancies v
GROUP BY coalesce(v.source, 'hh');
