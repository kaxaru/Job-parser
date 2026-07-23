"""Адаптер ClickHouse: широкий факт hh.vacancies (HTTP JSONEachRow); витрины
наполняются инкрементальными MV на вставке — без REFRESH."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from ..domain import Vacancy

BATCH = 2000
# таблицы, которые чистим перед полным перезаливом (факт + витрины)
_TRUNCATE = ("hh.vacancies", "hh.skill_demand", "hh.salary_by_exp", "hh.source_stats")


class ClickHouseWarehouse:
    name = "clickhouse"

    def __init__(self, url: str, schema_sql: Path):
        self.url = url if url.endswith("/") else url + "/"
        self.schema_sql = Path(schema_sql)

    def _post(self, query: str, body: str | None = None) -> str:
        if body is None:                          # DDL/DML: SQL в теле
            url, data = self.url, query.encode("utf-8")
        else:                                     # INSERT ... FORMAT: SQL в query, строки в теле
            url = self.url + "?" + urllib.parse.urlencode({"query": query})
            data = body.encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.read().decode()

    def init_schema(self) -> None:
        # ClickHouse по HTTP исполняет по одному стейтменту — бьём файл по ';'.
        # Сначала срезаем '--'-комментарии: ';' внутри них иначе ломает разбиение.
        sql = re.sub(r"--[^\n]*", "", self.schema_sql.read_text(encoding="utf-8"))
        for stmt in sql.split(";"):
            if stmt.strip():
                self._post(stmt)

    def load(self, vacancies: list[Vacancy]) -> int:
        for t in _TRUNCATE:
            self._post(f"TRUNCATE TABLE IF EXISTS {t}")
        batch, total = [], 0
        for v in vacancies:
            batch.append(json.dumps({
                "id": v.id, "source": v.source, "name": v.name, "city": v.city, "employer": v.employer,
                "salary_min": v.salary_min, "salary_max": v.salary_max,
                "salary_currency": v.salary_currency,
                "salary_gross": None if v.salary_gross is None else int(v.salary_gross),
                "experience": v.experience, "schedule": v.schedule,
                "is_remote": int(v.is_remote), "url": v.url, "query": v.query,
                "skills": list(v.skills),
            }, ensure_ascii=False))
            if len(batch) >= BATCH:
                total += self._flush(batch)
        total += self._flush(batch)
        return total

    def _flush(self, batch: list[str]) -> int:
        if not batch:
            return 0
        self._post("INSERT INTO hh.vacancies FORMAT JSONEachRow", "\n".join(batch))
        n = len(batch)
        batch.clear()
        return n

    def count(self) -> int:
        return int(self._post("SELECT count() FROM hh.vacancies").strip())
