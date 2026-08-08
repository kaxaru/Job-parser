"""Бенч: сравнение подходов поиска на ОДНИХ данных.
Показывает EXPLAIN ANALYZE (тип скана + время) и медианную задержку по N прогонам.
Объём выборки НЕ захардкожен — читается из таблицы и подставляется в заголовок отчёта.
Запуск: .venv3/Scripts/python.exe dwh_demo/search_demo/bench.py

Вывод — ASCII (консоль Windows коверкает кириллицу); полный отчёт пишется в REPORT.md (UTF-8).
"""
import re
import statistics
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hrwork.config import PG_DSN  # единый источник DSN (грузит .env)

N = 25  # прогонов для медианы
OUT = Path(__file__).resolve().parent / "REPORT.md"

CITY, SALARY, TERM = "Москва", 200000, "python backend"

# Порог зарплаты — В РУБЛЯХ, и сравнивается он с рублёвой колонкой `sal_mid_rub`:
# `sal_mid` держит валюту портала, и «>= 200 000» по нему выбрасывало все долларовые
# вилки, зато пропускало 50 000 000 UZS (см. комментарий к колонке в load.py).
# (label, sql, params)
MAIN = {
    # честный аналог 'python & backend': оба слова в полном тексте (name+description).
    # выражение по конкатенации не ложится на индекс -> Seq Scan (в этом и урок).
    "Q0_ILIKE_naive": (
        "SELECT id, name, sal_mid_rub FROM search_demo.vacancies "
        "WHERE (name || ' ' || coalesce(description,'')) ILIKE %s "
        "  AND (name || ' ' || coalesce(description,'')) ILIKE %s "
        "  AND city = %s AND sal_mid_rub >= %s LIMIT 20",
        ("%python%", "%backend%", CITY, SALARY),
    ),
    "Q1_tsvector_GIN": (
        "SELECT id, name, sal_mid_rub, ts_rank(doc, q) AS rank "
        "FROM search_demo.vacancies, websearch_to_tsquery('russian', %s) q "
        "WHERE doc @@ q AND city = %s AND sal_mid_rub >= %s "
        "ORDER BY rank DESC LIMIT 20",
        (TERM, CITY, SALARY),
    ),
}


def scan_kind(plan: str) -> str:
    for k in ("Seq Scan", "Parallel Seq Scan", "Bitmap Index Scan",
              "Bitmap Heap Scan", "Index Scan", "Index Only Scan"):
        if k in plan:
            return k
    return "?"


def exec_time(plan: str) -> float:
    m = re.search(r"Execution Time: ([\d.]+) ms", plan)
    return float(m.group(1)) if m else float("nan")


def explain(cur, sql, params) -> str:
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
    return "\n".join(r[0] for r in cur.fetchall())


def median_ms(cur, sql, params, n=N) -> float:
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        cur.execute(sql, params)
        cur.fetchall()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def main():
    conn = psycopg2.connect(**PG_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    out = []  # строки для REPORT.md

    def emit(line=""):
        print(line)
        out.append(line)

    # Объём выборки — из самой таблицы: захардкоженные «9.7k» в заголовке отчёта
    # пережили рост данных на порядок и врали в файле, который док велит считать истиной.
    cur.execute("SELECT count(*) FROM search_demo.vacancies")
    total = cur.fetchone()[0]
    emit(f"# PoC поиска: PostgreSQL tsvector — измерения на {total} вакансиях\n")
    emit(f"Запрос-кейс: «{TERM}» в городе {CITY} с зарплатой >= {SALARY} руб. "
         f"(колонка sal_mid_rub — рублёвый эквивалент, валюты сведены по курсу).\n")

    emit("## 1. Основной кейс: наивный ILIKE vs tsvector+GIN\n")
    for label, (sql, params) in MAIN.items():
        plan = explain(cur, sql, params)
        med = median_ms(cur, sql, params)
        cur.execute(f"SELECT count(*) FROM ({sql.replace(' LIMIT 20', '')}) z", params)
        found = cur.fetchone()[0]
        emit(f"### {label}")
        emit(f"- scan: **{scan_kind(plan)}**")
        emit(f"- EXPLAIN ANALYZE Execution Time: **{exec_time(plan):.3f} ms**")
        emit(f"- медиана {N} прогонов (с round-trip): **{med:.3f} ms**")
        emit(f"- найдено строк: {found}")
        emit("")

    # 2. Точность: ILIKE substring ловит мусор ('java' -> 'javascript'), токен tsquery — нет
    emit("## 2. Точность: 'java' (подстрока ILIKE vs токен tsquery)\n")
    cur.execute("SELECT count(*) FROM search_demo.vacancies WHERE description ILIKE %s", ("%java%",))
    ilike_java = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM search_demo.vacancies WHERE doc @@ to_tsquery('russian', %s)", ("java",))
    ts_java = cur.fetchone()[0]
    emit(f"- ILIKE '%java%'                : {ilike_java} строк (включая ложные 'javascript', 'java-script' …)")
    emit(f"- tsquery 'java' (отдельный токен): {ts_java} строк")
    emit(f"- разница (ложные срабатывания ILIKE): **{ilike_java - ts_java}**\n")

    # 3. Морфология: один токен ловит все словоформы
    emit("## 3. Морфология русского: 1 токен = все падежи\n")
    cur.execute(
        "SELECT count(*) FROM search_demo.vacancies WHERE doc @@ to_tsquery('russian', %s)",
        ("программист",))
    morph = cur.fetchone()[0]
    emit(f"- tsquery 'программист' матчит программист/-а/-ов/-ы (стемминг): {morph} строк\n")

    # 4. Опечатки: trigram word_similarity + оператор <% (индексируется), а не similarity()
    emit("## 4. Опечатки: trigram word_similarity ('pyton' -> Python)\n")
    cur.execute("SET pg_trgm.word_similarity_threshold = 0.5;")
    sql_trgm = ("SELECT DISTINCT name, round(word_similarity(%s, name)::numeric, 2) ws "
                "FROM search_demo.vacancies WHERE %s <%% name "
                "ORDER BY ws DESC LIMIT 5")
    plan = explain(cur, sql_trgm, ("pyton", "pyton"))
    cur.execute(sql_trgm, ("pyton", "pyton"))
    rows = cur.fetchall()
    emit(f"- scan: **{scan_kind(plan)}** (trigram-GIN), {exec_time(plan):.3f} ms")
    emit(f"- топ похожих на 'pyton': {[r[0][:42] for r in rows][:3]}\n")

    # 5. Фасеты (агрегации) — то, ради чего часто берут ES; в PG это обычный GROUP BY
    emit("## 5. Фасеты (GROUP BY) — счётчики для UI-фильтров\n")
    sql_facet = ("SELECT t, count(*) c FROM search_demo.vacancies, unnest(techs) t "
                 "GROUP BY t ORDER BY c DESC LIMIT 5")
    med_f = median_ms(cur, sql_facet, None)
    cur.execute(sql_facet)
    emit(f"- топ-стек (unnest techs, медиана {N}): **{med_f:.3f} ms** -> {cur.fetchall()}")
    emit("")

    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"\n[report -> {OUT}]")
    conn.close()


if __name__ == "__main__":
    main()
