# Power BI + DAX поверх MS SQL (закрытие гэпа вакансии)

Power BI Desktop — **Windows-приложение, его нельзя контейнеризовать** (нет Docker-образа).
Поэтому: данные готовит наш ETL в MS SQL, а дашборд `.pbix` собираешь в Power BI Desktop
по этой инструкции. Это закрывает связку из вакансии: **T-SQL + Power BI + DAX** на одном стеке.

## 0. Предусловие — данные в MS SQL

```bash
cd dwh_demo
docker compose up -d mssql
python -m etl -t mssql all        # звезда в БД hh: core.vacancies + mart.*
```

## 1. Установить Power BI Desktop

Бесплатно: Microsoft Store → «Power BI Desktop», или https://aka.ms/pbidesktopstore.
(Только Windows. Аналог под macOS/Linux — нет; учат именно на нём.)

## 2. Подключиться к нашему MS SQL

Power BI Desktop → **Получить данные → SQL Server** (`Get Data → SQL Server database`):

| Поле | Значение |
|------|----------|
| Server | `localhost,1433` |
| Database | `hh` |
| Режим | **Import** (для DAX-мер; DirectQuery — если нужен live) |

Аутентификация → **Database** → Username `sa`, Password `DwhDemo2026!`
(сертификат — «Доверять»/Trust, у локального MS SQL self-signed).

Выбрать таблицы: `core.vacancies`, `core.cities`, `core.employers`, `core.skills`,
`core.vacancy_skills` (звезда — Power BI сам подтянет связи по FK) или готовые `mart.*`.

## 3. DAX-меры (вкладка Modeling → New measure)

Это и есть навык «Power BI + DAX». Меры считаются на лету по модели:

```dax
-- всего вакансий (базовая мера)
Вакансий = COUNTROWS('core vacancies')

-- удалённые (CALCULATE + фильтр — ядро DAX)
Удалённых = CALCULATE([Вакансий], 'core vacancies'[is_remote] = TRUE())

-- доля удалёнки (DIVIDE безопасно делит на ноль)
Доля удалёнки % = DIVIDE([Удалённых], [Вакансий], 0) * 100

-- с зарплатой
С зарплатой = CALCULATE([Вакансий],
    NOT ISBLANK('core vacancies'[salary_min]) || NOT ISBLANK('core vacancies'[salary_max]))

-- средняя верхняя зарплата (игнорит пустые)
Средняя ЗП макс = AVERAGE('core vacancies'[salary_max])

-- медиана (DAX MEDIAN)
Медиана ЗП макс = MEDIAN('core vacancies'[salary_max])
```

## 4. Визуализации (демонстрация «Full Stack BI»)

- **Карточки (Card):** `[Вакансий]`, `[Удалённых]`, `[Доля удалёнки %]`.
- **Bar chart:** ось = `core skills[name]` (через связь vacancy_skills), значение = `[Вакансий]`
  → «Спрос на навыки» (аналог нашего mart.skill_demand).
- **Column chart:** ось = `core vacancies[experience]`, значение = `[Средняя ЗП макс]`
  → «Зарплата по опыту».
- **Map / Table:** `core cities[name]` + `[Вакансий]` + `[Доля удалёнки %]`.
- **Slicer (фильтр):** `core cities[name]` и `core vacancies[experience]` — интерактив,
  как фильтры Город/Опыт в нашем Metabase-дашборде.

## 5. Сохранить и версионировать

`Файл → Сохранить` → `powerbi/hh-market.pbix`. `.pbix` — бинарный, но кладётся в git
(не игнорируется). Для портфолио можно экспортнуть скриншоты/PDF.

---

## Зачем это на собесе

Закрывает **3 требования вакансии разом** на одном стеке:
- **T-SQL** — звезда и витрины написаны на T-SQL (`etl/sql/mssql/schema.sql`: IDENTITY, MERGE, NVARCHAR, views).
- **DWH/Kimball** — факт `core.vacancies` + измерения `cities/employers/skills` (схема звезда).
- **Power BI + DAX** — модель + меры (CALCULATE, DIVIDE, MEDIAN) + интерактивные срезы.

**Фраза:** «Тот же ETL, что наполняет Postgres/ClickHouse, через адаптер грузит и в MS SQL
(T-SQL: MERGE для измерений, NVARCHAR для кириллицы, views под витрины). Поверх —
Power BI Desktop с DAX-мерами и слайсерами Город/Опыт. Это полный Full-Stack BI цикл
от сырья до интерактивного дашборда на Microsoft-стеке».
