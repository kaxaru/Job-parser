"""Фасад над REST API Metabase — единственная точка работы с HTTP.

Прячет аутентификацию, подключения к БД, создание карточек и сборку дашбордов.
Раньше это копипастилось в каждом скрипте; теперь — один клиент, инжектится в дашборды.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class MetabaseClient:
    def __init__(self, base: str, email: str, password: str):
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.token: str | None = None

    # ── низкий уровень ──
    def _api(self, method: str, path: str, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Metabase-Session", self.token)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    # ── аутентификация ──
    def connect(self) -> None:
        """Первичный setup (если инстанс пустой) либо логин."""
        # Ошибку `/api/session/properties` НЕ считаем фатальной: инстанс может быть
        # настроен, и обычный логин ниже сам скажет, что не так. Важно лишь не звать
        # `.get` на теле-строке 4xx/5xx — до 09.08.2026 это падало AttributeError.
        _, props = self._api("GET", "/api/session/properties")
        token = props.get("setup-token") if isinstance(props, dict) else None
        if token:
            st, res = self._api("POST", "/api/setup", {
                "token": token,
                "user": {"first_name": "Demo", "last_name": "User",
                         "email": self.email, "password": self.password, "site_name": "HH DWH"},
                "prefs": {"site_name": "HH DWH", "allow_tracking": False},
            })
            if st == 200:
                self.token = res["id"]
                return
        st, res = self._api("POST", "/api/session",
                            {"username": self.email, "password": self.password})
        if st != 200:
            raise RuntimeError(f"Metabase auth failed: {st} {res}")
        self.token = res["id"]

    # ── подключения к хранилищам ──
    def find_database(self, name: str, engine: str):
        """Id подключения по (имя, движок) или None, если такого нет.

        Fail fast на ошибке HTTP: `_api` отдаёт тело 4xx/5xx СТРОКОЙ, и до 09.08.2026
        она молча уезжала в `d.get(...)` — провижининг падал `AttributeError: 'str'
        object has no attribute 'get'`, не назвав ни статуса, ни ответа Metabase."""
        st, dbs = self._api("GET", "/api/database")
        if st != 200:
            raise RuntimeError(f"database list failed: {st} {dbs}")
        items = dbs.get("data", dbs) if isinstance(dbs, dict) else dbs
        if not isinstance(items, list):
            raise RuntimeError(f"database list: неожиданный формат ответа: {dbs!r}")
        return next((d["id"] for d in items
                     if d.get("name") == name and d.get("engine") == engine), None)

    def ensure_database(self, name: str, engine: str, details: dict) -> int:
        """Подключение к движку: найденное переиспользуется, отсутствующее создаётся.

        Идемпотентность держится на `find_database`: повторный провижининг не должен
        заводить второе подключение к тому же движку."""
        db_id = self.find_database(name, engine)
        if db_id is None:
            st, res = self._api("POST", "/api/database",
                                {"engine": engine, "name": name, "details": details})
            if st not in (200, 202):
                raise RuntimeError(f"database '{name}' failed: {st} {res}")
            db_id = res["id"]
            self._api("POST", f"/api/database/{db_id}/sync_schema")
        return db_id

    def run_sql(self, db_id: int, sql: str) -> list:
        """Выполнить native-SQL и вернуть строки (для списков значений фильтров)."""
        _, res = self._api("POST", "/api/dataset",
                          {"type": "native", "native": {"query": sql}, "database": db_id})
        return (res or {}).get("data", {}).get("rows", []) if isinstance(res, dict) else []

    # ── карточки и дашборды ──
    def create_card(self, name, db_id, sql, display, viz=None, tags=None) -> int:
        native = {"query": sql}
        if tags:
            native["template-tags"] = tags
        st, res = self._api("POST", "/api/card", {
            "name": name, "display": display,
            "dataset_query": {"type": "native", "native": native, "database": db_id},
            "visualization_settings": viz or {}})
        if st not in (200, 202):
            raise RuntimeError(f"card '{name}' failed: {st} {res}")
        return res["id"]

    def upsert_dashboard(self, title: str) -> int:
        """Вернуть id дашборда; если уже есть — очистить (старые карточки в архив).
        Делает повторный провижининг идемпотентным.

        Fail fast на ошибке HTTP — как в `find_database`: `_api` отдаёт тело 4xx/5xx
        СТРОКОЙ, и она молча уезжала в `d.get(...)`/`res["id"]`, роняя провижининг
        на `AttributeError: 'str' object has no attribute 'get'` и `TypeError: string
        indices must be integers` вместо статуса и ответа Metabase."""
        st, dl = self._api("GET", "/api/dashboard")
        if st != 200:
            raise RuntimeError(f"dashboard list failed: {st} {dl}")
        items = dl.get("data", dl) if isinstance(dl, dict) else dl
        if not isinstance(items, list):
            raise RuntimeError(f"dashboard list: неожиданный формат ответа: {dl!r}")
        d_id = next((d["id"] for d in items if d.get("name") == title), None)
        if d_id is None:
            # 202 — асинхронное создание, такой же успех, как 200 (см. create_card).
            st, res = self._api("POST", "/api/dashboard", {"name": title})
            if st not in (200, 202):
                raise RuntimeError(f"dashboard '{title}' failed: {st} {res}")
            return res["id"]
        _, full = self._api("GET", f"/api/dashboard/{d_id}")
        for dc in (full or {}).get("dashcards", []):
            cid = dc.get("card_id")
            if cid:
                self._api("PUT", f"/api/card/{cid}", {"archived": True})
        self._api("PUT", f"/api/dashboard/{d_id}", {"dashcards": []})
        return d_id

    def set_dashboard(self, d_id: int, dashcards: list, parameters=None) -> None:
        payload = {"dashcards": dashcards}
        if parameters is not None:
            payload["parameters"] = parameters
        st, res = self._api("PUT", f"/api/dashboard/{d_id}", payload)
        if st != 200:
            raise RuntimeError(f"dashboard {d_id} update failed: {st} {res}")
