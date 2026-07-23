"""Локальный сервер ленты с автосохранением отметок: python hh.py serve.

Только stdlib (http.server), без новых зависимостей.

Архитектура: ответ — это ЗНАЧЕНИЕ (Resp); прикладные хендлеры (marks/search/страница)
возвращают Resp и не трогают сокет, а пишет его ОДИН метод _write. Маршрутизация —
таблица «путь -> метод-хендлер». Статику из data/ отдаём с gzip+ETag (сжатое кэшируется
по mtime, чтобы не пере-сжимать 145 МБ feed-desc.js на каждый запрос); что не газипим —
стримит stdlib через super().do_GET().

  GET  /api/marks   -> текущий marks.json
  POST /api/marks   -> перезаписать marks.json (тело — JSON {id: status})
  GET  /api/search  -> PG full-text (tsvector), ранжированный JSON
Источник правды на диске (data/marks.json) переживает пересбор данных.
"""
import gzip
import json
import mimetypes
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from hrwork.application.apply.chat import chat_class
from hrwork.application.apply.runtime.store import store
from hrwork.config import DATA_DIR, SERVE_PORT, log

_API = "/api/marks"
_API_SEARCH = "/api/search"
_API_APPLY = "/api/apply"
_API_FORMS = "/api/forms"          # вакансии-опросники (нужна ручная форма)
_API_STATUSES = "/api/statuses"    # статусы откликов с HH (currentApplicantState)
_API_APPLIED = "/api/applied"      # журнал откликов (крон+лента) с таймстампами
_API_CHATS = "/api/chats"          # переписка: что ответил работодатель и ждёт ли он ответа
_SRC_DIR = DATA_DIR.parent / "src"          # фронт-исходники (страница поиска)
_GZIP_EXT = {".html", ".js", ".css", ".json", ".svg", ".txt"}   # текст -> жмём
_MAX_BODY = 1 << 20                          # 1 МБ — потолок тела POST (marks.json мал)

# Кэш сжатых файлов: путь -> (mtime, size, gz-байты). Инвалидируется по mtime/size.
_GZIP_CACHE: dict[str, tuple[float, int, bytes]] = {}
_GZIP_LOCK = Lock()


class Resp(NamedTuple):
    """Ответ как значение. Хендлеры его ВОЗВРАЩАЮТ, в сокет пишет один _write —
    это развязывает «что отвечаем» от «как пишем» и делает хендлеры тестируемыми."""
    status: int
    body: bytes = b""
    ctype: str | None = None
    headers: dict | None = None


def _json(code: int, obj) -> Resp:
    return Resp(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8", {"Cache-Control": "no-store"})


def _etag(st) -> str:
    return f'"{int(st.st_mtime)}-{st.st_size}"'


def _gzipped(fpath: Path, st) -> bytes:
    """gz-байты файла из кэша; читаем и сжимаем лишь при промахе (сменился mtime/size)."""
    key = str(fpath)
    hit = _GZIP_CACHE.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    body = gzip.compress(fpath.read_bytes(), compresslevel=6)
    with _GZIP_LOCK:
        _GZIP_CACHE[key] = (st.st_mtime, st.st_size, body)
    return body


class _Handler(SimpleHTTPRequestHandler):
    # ───────────────────────── маршрутизация ─────────────────────────
    def do_GET(self):
        route = _GET_ROUTES.get(self._path())
        if route:
            return self._write(route(self))          # точный маршрут -> Resp
        return self._serve_static()                  # всё прочее -> статика из data/

    def do_POST(self):
        path = self._path()
        if path == _API:
            return self._write(self._marks_post())
        if path == _API_APPLY:
            return self._write(self._apply_post())
        return self._write(Resp(404))

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    # ─────────────────── прикладные хендлеры (-> Resp) ───────────────────
    def _marks_get(self) -> Resp:
        return _json(200, store.marks())

    def _forms_get(self) -> Resp:
        """Форм-очередь с диска — фронт накладывает бейдж «форма» БЕЗ пересборки ленты."""
        return _json(200, store.forms())

    def _statuses_get(self) -> Resp:
        """Статусы откликов с HH — фронт освежает CRM-бейджи без пересборки ленты."""
        return _json(200, store.statuses())

    def _chats_get(self) -> Resp:
        """Свёртка переписки: {vacancyId: {kind, sender, needs_reply, can_write, preview, …}}.
        Индекс шаблонов строим по ВСЕМУ корпусу — иначе рассылку от имени живого рекрутера
        не отличить от личного письма. Наполняет sync_statuses; лента берёт без пересборки."""
        chats = store.chat_messages()
        templates = chat_class.build_template_index(chats)
        out = {vid: chat_class.analyze(d.get("messages") or [], templates, d.get("write"))
               for vid, d in chats.items()}
        # kind=none (последнее слово за нами) отсекаем, НО чат с контактом отдаём всегда:
        # телефон/телеграм рекрутёра не протухает после нашего ответа (инцидент 2026-07-22:
        # телефонный контакт пропал из фильтра «С контактами» после синка переписки)
        return _json(200, {vid: a for vid, a in out.items()
                           if a.get("kind") != "none" or a.get("contact")})

    def _applied_get(self) -> Resp:
        """Журнал откликов (id/name/url/ts/via/status) — для календарного вида «мои отклики»."""
        return _json(200, store.applied_log())

    def _marks_post(self) -> Resp:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return Resp(400, b"bad content-length")
        if n > _MAX_BODY:
            return Resp(413, b"payload too large")
        try:
            marks = json.loads(self.rfile.read(n) if n else b"{}")
        except json.JSONDecodeError:
            return Resp(400, b"bad json")
        if not isinstance(marks, dict):
            return Resp(400, b"expected object")
        store.set_marks(marks)
        return Resp(204)

    def _apply_post(self) -> Resp:
        """POST /api/apply {id, url, cover} -> отклик на вакансию в фоне через Playwright.
        Тело: JSON. Ответ: {status: applied|already|form|skip|busy|no-session|error, letter}.
        Отклик реальный и небыстрый (браузер + DDoS-Guard) — клиент ждёт."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return Resp(400, b"bad content-length")
        if n > _MAX_BODY:
            return Resp(413, b"payload too large")
        try:
            body = json.loads(self.rfile.read(n) if n else b"{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "bad json"})
        vid = str(body.get("id") or "").strip()
        if not vid:
            return _json(400, {"error": "no id"})
        try:
            # тёплый воркер: один браузер переиспользуется между кликами, закрывается
            # по простою (не поднимаем Playwright заново на каждый отклик).
            from hrwork.application.apply.autoclick import get_apply_worker
            url, cover, name = body.get("url", ""), body.get("cover", ""), body.get("name", "")
            res = get_apply_worker().submit(vid, url, cover, name)
            if res.get("status") == "busy":
                # браузер занят кроном -> кладём в очередь ожидания, крон дожмёт (26+)
                pos = store.enqueue(vid, url, name, cover)
                return _json(200, {"status": "queued", "position": pos, "letter": False})
            return _json(200, res)
        except Exception as e:
            log.exception("apply failed")
            return _json(500, {"status": "error", "error": f"{type(e).__name__}: {e}"})

    def _search(self) -> Resp:
        """/api/search?q=&city=&sal=&fresh=&limit=&offset= -> ранжированный JSON (PG tsvector).
        БД недоступна -> 503 (лента продолжает работать)."""
        qs = parse_qs(urlparse(self.path).query)

        def one(k, d=None):
            return (qs.get(k) or [d])[0]

        from hrwork.infrastructure import search as search_mod
        try:
            res = search_mod.search(q=one("q"), city=one("city"), sal_min=one("sal", 0),
                                    fresh=one("fresh"), source=one("source"),
                                    limit=one("limit", 20), offset=one("offset", 0))
            return _json(200, res)
        except search_mod.SearchUnavailable as e:
            return _json(503, {"error": str(e),
                               "hint": "подними hh-postgres и прогони "
                                       "dwh_demo/search_demo/load.py"})
        except Exception as e:
            log.exception("search failed")            # трейс в лог сервера (observability)
            return _json(500, {"error": f"{type(e).__name__}: {e}"})

    def _search_page(self) -> Resp:
        fpath = _SRC_DIR / "search.html"
        if not fpath.is_file():
            return Resp(404)
        return Resp(200, fpath.read_bytes(), "text/html; charset=utf-8")

    # ──────────── статика data/: gzip+ETag, иначе стрим через super() ────────────
    def _serve_static(self):
        if self.path in ("/", ""):
            self.path = "/feed.html"
        gz = self._gzip_resp()
        if gz is not None:
            return self._write(gz)
        return super().do_GET()                       # не-текст / без gzip -> стрим stdlib

    def _gzip_resp(self) -> "Resp | None":
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            return None
        fpath = Path(self.translate_path(self.path))  # безопасный путь (без traversal)
        if fpath.suffix.lower() not in _GZIP_EXT or not fpath.is_file():
            return None
        st = fpath.stat()
        etag = _etag(st)
        if self.headers.get("If-None-Match") == etag:  # у клиента уже актуальная версия
            return Resp(304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        return Resp(200, _gzipped(fpath, st), f"{ctype}; charset=utf-8",
                    {"Content-Encoding": "gzip", "ETag": etag, "Cache-Control": "no-cache"})

    # ─────────────────── единственный писатель в сокет ───────────────────
    def _write(self, r: Resp):
        self.send_response(r.status)
        if r.ctype:
            self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(r.body)))
        for k, v in (r.headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if r.body and self.command != "HEAD":
            self.wfile.write(r.body)

    def log_request(self, code="-", size="-"):
        # Шумный per-request access-лог глушим; через loguru логируем ТОЛЬКО проблемы
        # (4xx/5xx), нормальные 2xx молчат. Сюда проходит каждый send_response.
        if isinstance(code, int) and code >= 400:
            log.warning("{} {} -> {}", self.command, self.path, code)

    def log_message(self, *_):  # дубль из log_error/send_error -> глушим (логируем в log_request)
        pass


# Точные GET-маршруты: путь -> метод-хендлер (возвращает Resp). Определяем ПОСЛЕ класса,
# чтобы ссылаться на методы напрямую — без lambda и без обращений к приватам из модуля.
_GET_ROUTES = {
    _API:           _Handler._marks_get,
    _API_FORMS:     _Handler._forms_get,
    _API_STATUSES:  _Handler._statuses_get,
    _API_APPLIED:   _Handler._applied_get,
    _API_CHATS:     _Handler._chats_get,
    _API_SEARCH:    _Handler._search,
    "/search":      _Handler._search_page,
    "/search.html": _Handler._search_page,
}


def run_server(port: int = SERVE_PORT):
    handler = partial(_Handler, directory=str(DATA_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    log.success("Лента: http://127.0.0.1:{}/   (Ctrl+C — стоп)", port)
    log.info("Поиск (PG full-text): http://127.0.0.1:{}/search   (нужен hh-postgres + search_demo)", port)
    log.info("Отметки автосохраняются в {}", DATA_DIR / "marks.json")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Остановка сервера…")
    finally:
        httpd.shutdown()
        httpd.server_close()
