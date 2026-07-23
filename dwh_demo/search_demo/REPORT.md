# PoC поиска: PostgreSQL tsvector — измерения на 9.7k вакансий

Запрос-кейс: «python backend» в городе Москва с зарплатой >= 200000.

## 1. Основной кейс: наивный ILIKE vs tsvector+GIN

### Q0_ILIKE_naive
- scan: **Bitmap Index Scan**
- EXPLAIN ANALYZE Execution Time: **25.485 ms**
- медиана 25 прогонов (с round-trip): **24.614 ms**
- найдено строк: 47

### Q1_tsvector_GIN
- scan: **Bitmap Index Scan**
- EXPLAIN ANALYZE Execution Time: **2.110 ms**
- медиана 25 прогонов (с round-trip): **2.682 ms**
- найдено строк: 45

## 2. Точность: 'java' (подстрока ILIKE vs токен tsquery)

- ILIKE '%java%'                : 2256 строк (включая ложные 'javascript', 'java-script' …)
- tsquery 'java' (отдельный токен): 1356 строк
- разница (ложные срабатывания ILIKE): **900**

## 3. Морфология русского: 1 токен = все падежи

- tsquery 'программист' матчит программист/-а/-ов/-ы (стемминг): 3173 строк

## 4. Опечатки: trigram word_similarity ('pyton' -> Python)

- scan: **Bitmap Index Scan** (trigram-GIN), 18.303 ms
- топ похожих на 'pyton': ['QA Automation (Python)', 'Senior QA automation Python specialist', 'Инженер по автоматизации тестирования / Au']

## 5. Фасеты (GROUP BY) — счётчики для UI-фильтров

- топ-стек (unnest techs, медиана 25): **28.603 ms** -> [('1С', 6154), ('Python', 4540), ('PostgreSQL', 3254), ('Docker', 2678), ('ML/AI', 2227)]
