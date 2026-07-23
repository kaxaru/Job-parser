"""Тесты локального сервера ленты (hh.py serve): маршрутизация, gzip-кэш, лимит тела POST.

Проверяем ПРИКЛАДНОЙ слой через Resp-архитектуру — без живого сокета. _Handler наследует
SimpleHTTPRequestHandler, чей __init__ сразу вычитывает запрос из сокета, поэтому instance
строим через __new__ и выставляем ТОЛЬКО те атрибуты, которые читают хендлеры
(path/command/headers/rfile/directory). headers — обычный dict: код зовёт лишь .get(...).
"""
import gzip
import io

import pytest

from hrwork.presentation import server
from hrwork.presentation.server import Resp, _Handler


def _bare_handler(path="/", command="GET", headers=None, body=b"", directory=None):
    """_Handler без __init__ (иначе он полез бы слушать сокет). Ставим то, что нужно хендлеру."""
    h = _Handler.__new__(_Handler)
    h.path = path
    h.command = command
    h.headers = headers or {}          # dict достаточно: код обращается только через .get(...)
    h.rfile = io.BytesIO(body)
    if directory is not None:
        h.directory = directory        # translate_path маппит URL -> файл относительно него
    return h


@pytest.fixture(autouse=True)
def _isolate_gzip_cache():
    # _GZIP_CACHE — модульный глобал; чистим вокруг каждого теста, чтобы кэш не протекал между ними.
    server._GZIP_CACHE.clear()
    yield
    server._GZIP_CACHE.clear()


# ───────────────────────────── маршрутизация ─────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("/api/marks?id=1&x=2", "/api/marks"),   # query отрезается
    ("/api/marks/",          "/api/marks"),   # хвостовой слэш снимается
    ("/api/marks",           "/api/marks"),   # канон остаётся собой
    ("/search/?q=py",        "/search"),       # слэш + query вместе
    ("/",                    "/"),              # корень
    ("",                     "/"),              # пусто -> корень (rstrip опустошил бы путь)
])
def test_path_strips_query_and_trailing_slash(raw, expected):
    # Нормализация пути — то, по чему потом ищется маршрут; ломается она -> ломается роутинг.
    assert _bare_handler(path=raw)._path() == expected


@pytest.mark.parametrize("path, handler", [
    (server._API,          _Handler._marks_get),
    (server._API_FORMS,    _Handler._forms_get),
    (server._API_STATUSES, _Handler._statuses_get),
    (server._API_APPLIED,  _Handler._applied_get),
    (server._API_CHATS,    _Handler._chats_get),
    (server._API_SEARCH,   _Handler._search),
    ("/search",            _Handler._search_page),
    ("/search.html",       _Handler._search_page),
])
def test_known_get_paths_map_to_expected_handlers(path, handler):
    # Тест-страж согласованности таблицы маршрутов: путь -> ровно тот метод, что задуман.
    assert server._GET_ROUTES[path] is handler


def test_do_get_dispatches_known_path_to_its_registered_handler(monkeypatch):
    # _GET_ROUTES держит ССЫЛКИ на функции, снятые на этапе определения класса, поэтому
    # подменять маршрут надо через setitem словаря, а не setattr метода класса.
    sentinel = Resp(299, b"routed")
    monkeypatch.setitem(server._GET_ROUTES, server._API, lambda self: sentinel)
    h = _bare_handler(path=server._API)
    captured = []
    h._write = captured.append          # единственный писатель в сокет -> перехватываем Resp
    h.do_GET()
    assert captured == [sentinel]


def test_unknown_get_path_falls_through_to_static():
    # Неизвестный GET не идёт в _write/_GET_ROUTES, а отдаётся статике из data/ (stdlib-стрим).
    h = _bare_handler(path="/definitely-not-a-route")
    called = []
    h._serve_static = lambda: called.append(True)
    h.do_GET()
    assert called == [True]


def test_unknown_post_path_returns_404():
    # POST по несуществующему пути -> явный Resp(404), без обращения к хендлерам.
    h = _bare_handler(path="/api/nope", command="POST")
    captured = []
    h._write = captured.append
    h.do_POST()
    assert captured[0].status == 404


@pytest.mark.parametrize("path, attr, marker", [
    (server._API,       "_marks_post", b"marks"),
    (server._API_APPLY, "_apply_post", b"apply"),
])
def test_known_post_paths_dispatch_to_their_handlers(path, attr, marker):
    # Каждый известный POST-путь уходит в СВОЙ хендлер (do_POST зовёт self._*_post() по имени).
    h = _bare_handler(path=path, command="POST")
    setattr(h, attr, lambda: Resp(299, marker))
    captured = []
    h._write = captured.append
    h.do_POST()
    assert captured[0] == Resp(299, marker)


# ─────────────────────────── лимит тела POST ───────────────────────────

@pytest.mark.parametrize("attr", ["_marks_post", "_apply_post"])
def test_oversized_post_body_rejected_with_413(attr):
    # Тело больше _MAX_BODY отбивается 413 ДО чтения rfile — оба POST-хендлера охраняют потолок.
    h = _bare_handler(command="POST",
                      headers={"Content-Length": str(server._MAX_BODY + 1)})
    r = getattr(h, attr)()
    assert r.status == 413


def test_at_limit_marks_body_is_accepted(monkeypatch):
    # Граница off-by-one: гейт это `>`, а не `>=` -> ровно _MAX_BODY должен проходить (204).
    # rfile отдаёт короткое тело b"{}" вместо целого мегабайта: проверяем, что гейт ПУСКАЕТ
    # запрос ровно на лимите, а не сам разбор — b"{}" стоит за любое валидное тело <= 1 МБ.
    monkeypatch.setattr(server.store, "set_marks", lambda full: None)
    h = _bare_handler(command="POST",
                      headers={"Content-Length": str(server._MAX_BODY)}, body=b"{}")
    assert h._marks_post().status == 204


@pytest.mark.parametrize("attr", ["_marks_post", "_apply_post"])
def test_bad_content_length_rejected_with_400(attr):
    # Нечисловой Content-Length -> 400, а не падение int() наверх.
    h = _bare_handler(command="POST", headers={"Content-Length": "not-a-number"})
    assert getattr(h, attr)().status == 400


def test_non_object_marks_body_rejected_with_400():
    # marks — это карта {id: status}; массив/скаляр отвергается (не затираем marks.json мусором).
    h = _bare_handler(command="POST", headers={"Content-Length": "7"}, body=b"[1,2,3]")
    assert h._marks_post() == Resp(400, b"expected object")


def test_bad_json_marks_body_rejected_with_400():
    # Битый JSON тела -> 400 bad json, а не 500 из необёрнутого json.loads.
    h = _bare_handler(command="POST", headers={"Content-Length": "1"}, body=b"{")
    assert h._marks_post() == Resp(400, b"bad json")


# ─────────────────────────── gzip-кэш ───────────────────────────

def test_gzipped_caches_compressed_bytes(tmp_path):
    # Первый вызов сжимает и кладёт в кэш; повторный при том же mtime/size отдаёт ТОТ ЖЕ объект
    # (идентичность доказывает попадание в кэш — повторное сжатие вернуло бы новый bytes).
    f = tmp_path / "feed.js"
    f.write_text("console.log(1);" * 100, encoding="utf-8")
    st = f.stat()
    first = server._gzipped(f, st)
    assert gzip.decompress(first) == f.read_bytes()
    assert str(f) in server._GZIP_CACHE
    assert server._gzipped(f, st) is first          # кэш-хит -> без пере-сжатия


def test_gzipped_reinvalidates_when_file_changes(tmp_path):
    # Смена содержимого (иной size) инвалидирует кэш -> отдаётся заново сжатый, другой объект.
    f = tmp_path / "feed.js"
    f.write_text("a" * 50, encoding="utf-8")
    first = server._gzipped(f, f.stat())
    f.write_text("bb" * 500, encoding="utf-8")       # размер изменился -> ключ кэша не совпадёт
    second = server._gzipped(f, f.stat())
    assert second is not first
    assert gzip.decompress(second) == f.read_bytes()


def test_gzip_resp_compresses_and_sets_gzip_headers(tmp_path):
    # Клиент умеет gzip + текстовый суффикс + файл есть -> Resp(200) со сжатым телом и заголовками.
    f = tmp_path / "feed.js"
    f.write_text("x" * 200, encoding="utf-8")
    h = _bare_handler(path="/feed.js", headers={"Accept-Encoding": "gzip"},
                      directory=str(tmp_path))
    r = h._gzip_resp()
    assert r.status == 200
    assert r.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(r.body) == f.read_bytes()
    assert len(server._GZIP_CACHE) == 1              # прошли через кэширующий _gzipped


def test_gzip_declined_without_accept_encoding_returns_none(tmp_path):
    # Клиент не заявил gzip -> None (наверх пойдёт стрим stdlib, а не сжатый ответ).
    h = _bare_handler(path="/feed.js", headers={}, directory=str(tmp_path))
    assert h._gzip_resp() is None


@pytest.mark.parametrize("suffix", [".png", ".ico", ".woff2"])
def test_gzip_skipped_for_non_text_suffix(suffix, tmp_path):
    # Бинарь (уже сжат) не газипим: короткое замыкание по суффиксу -> None (файл даже не нужен).
    h = _bare_handler(path=f"/asset{suffix}", headers={"Accept-Encoding": "gzip"},
                      directory=str(tmp_path))
    assert h._gzip_resp() is None


def test_gzip_returns_304_when_client_etag_matches(tmp_path):
    # У клиента актуальная версия (If-None-Match == ETag) -> 304 с пустым телом, без пере-отдачи.
    f = tmp_path / "feed.js"
    f.write_text("y" * 50, encoding="utf-8")
    etag = server._etag(f.stat())
    h = _bare_handler(path="/feed.js", directory=str(tmp_path),
                      headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    r = h._gzip_resp()
    assert r.status == 304
    assert r.body == b""
    assert r.headers["ETag"] == etag


def test_gzip_missing_file_returns_none(tmp_path):
    # Текстовый суффикс, но файла нет -> None (упадёт в stdlib-404, а не пустой gzip-200).
    h = _bare_handler(path="/missing.js", headers={"Accept-Encoding": "gzip"},
                      directory=str(tmp_path))
    assert h._gzip_resp() is None


def test_chats_endpoint_keeps_contact_after_our_reply(monkeypatch):
    # инцидент 2026-07-22: последнее слово за нами -> kind=none -> чат выпадал из /api/chats
    # ЦЕЛИКОМ, и телефон рекрутёра исчезал из фильтра «С контактами». Контакт не протухает.
    import json as _json_mod

    from hrwork.presentation import server as srv
    monkeypatch.setattr(srv.store, "chat_messages", lambda: {
        "1": {"messages": [
            {"text": "Звоните: +7 912 345-67-89", "mine": False, "ts": "t1"},
            {"text": "Спасибо, наберу", "mine": True, "ts": "t2"}]},
        "2": {"messages": [
            {"text": "Добрый день!", "mine": False, "ts": "t1"},
            {"text": "Здравствуйте!", "mine": True, "ts": "t2"}]},   # без контакта -> режется
    })
    h = _bare_handler(path=srv._API_CHATS)
    resp = h._chats_get()
    data = _json_mod.loads(resp.body)
    assert "1" in data and "+7 912 345-67-89" in data["1"]["contact"]
    assert "2" not in data                     # kind=none без контакта по-прежнему отсечён
