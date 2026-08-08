"""Тесты локального сервера ленты (hh.py serve): маршрутизация, gzip-кэш, лимит тела POST.

Проверяем ПРИКЛАДНОЙ слой через Resp-архитектуру — без живого сокета. _Handler наследует
SimpleHTTPRequestHandler, чей __init__ сразу вычитывает запрос из сокета, поэтому instance
строим через __new__ и выставляем ТОЛЬКО те атрибуты, которые читают хендлеры
(path/command/headers/rfile/directory). headers — обычный dict: код зовёт лишь .get(...).
"""
import gzip
import io
import json

import pytest

from hrwork.presentation import server
from hrwork.presentation.server import Resp, _Handler

_LOCAL = {"Host": "127.0.0.1:8000"}    # заголовки легитимного запроса «от своей же страницы»


def _bare_handler(path="/", command="GET", headers=None, body=b"", directory=None):
    """_Handler без __init__ (иначе он полез бы слушать сокет). Ставим то, что нужно хендлеру.

    `Host` подставляется по умолчанию: без него гард `_host_ok` (защита от DNS-rebinding)
    отбил бы КАЖДЫЙ запрос, и тесты проверяли бы только гард. Свои заголовки — явным
    аргументом; тесты гардов ниже так и делают."""
    h = _Handler.__new__(_Handler)
    h.path = path
    h.command = command
    h.headers = {**_LOCAL, **(headers or {})}   # dict достаточно: код зовёт только .get(...)
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


# ───────────── гарды источника: CSRF и DNS-rebinding (аудит 08.08.2026) ─────────────
# POST здесь ДЕЙСТВУЕТ: /api/apply шлёт необратимый отклик работодателю, /api/marks
# перезаписывает журнал отметок целиком. До фикса источник запроса не проверялся, и любая
# открытая вкладка могла отправить и то, и другое simple-запросом без preflight.


def _post(path=server._API_APPLY, headers=None):
    """POST через do_POST с перехватом Resp. Хендлер подменён: до него дойти НЕ должно,
    если гард сработал, — а если дошло, отличим по статусу 299."""
    h = _bare_handler(path=path, command="POST", headers=headers)
    h._marks_post = h._apply_post = lambda: Resp(299, b"reached handler")
    captured = []
    h._write = captured.append
    h.do_POST()
    return captured[0]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_post_from_another_site_is_rejected(site):
    # Sec-Fetch-Site шлют все современные браузеры; чужая страница -> 403 до хендлера.
    assert _post(headers={"Sec-Fetch-Site": site}).status == 403


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://evil.example:8000",
    "http://127.0.0.1.evil.example",     # петлевой адрес как ПРЕФИКС чужого домена
])
def test_post_with_foreign_origin_is_rejected(origin):
    assert _post(headers={"Origin": origin}).status == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_post_from_own_page_passes(site):
    # `none` — прямая навигация пользователем, не чужая страница.
    r = _post(headers={"Sec-Fetch-Site": site, "Origin": "http://127.0.0.1:8000"})
    assert r == Resp(299, b"reached handler")


def test_post_without_browser_headers_passes():
    """Ни Origin, ни Sec-Fetch-Site -> не браузер (curl, скрипт). Такой доступ
    задокументирован как принятый риск: закрываем именно браузерный вектор."""
    assert _post().status == 299


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "10.0.0.5:8000"])
def test_request_with_foreign_host_is_rejected(host):
    """DNS-rebinding: сокет слушает 127.0.0.1, но браузер приходит с доменом атакующего,
    и после ребиндинга его страница становится same-origin — то есть может ЧИТАТЬ ответы.
    Отбиваем и POST, и GET."""
    assert _post(headers={"Host": host}).status == 421
    h = _bare_handler(path=server._API_CHATS, headers={"Host": host})
    captured = []
    h._write = captured.append
    h.do_GET()
    assert captured[0].status == 421


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"])
def test_loopback_hosts_pass(host):
    """Петлевой Host пропускается СКВОЗЬ гард — до прикладного хендлера (сентинел 299).

    IPv6 в Host идёт в скобках — urlsplit их снимает, rsplit по ':' сломался бы.
    Проверяем наблюдаемый ответ, а не приватный `_host_ok()`: негативная сторона
    (`test_request_with_foreign_host_is_rejected`) уже ходит через do_POST, и позитивная
    обязана идти тем же путём — иначе перестановка гардов в do_POST осталась бы
    незамеченной (аудит 09.08.2026)."""
    assert _post(headers={"Host": host}) == Resp(299, b"reached handler")


# ───────────── раздача data/: белый список (аудит 08.08.2026, п.2) ─────────────

def _get(path, directory=None, headers=None):
    """GET через do_GET с перехватом Resp — наблюдаемый ответ вместо приватного предиката."""
    h = _bare_handler(path=path, directory=directory, headers=headers)
    captured = []
    h._write = captured.append
    h.do_GET()
    return captured[0]


@pytest.mark.parametrize("name", [
    "hh_state.json",          # живые куки сессии HH -> захват аккаунта
    "hh_token.json",          # OAuth-токен
    "chat_messages.json",     # переписка с рекрутёрами
    "talanto_contacts.json",  # телефоны и telegram рекрутёров: чужие ПДн
    "marks.json",
    "vacancies_raw.json",
    "reports/01_cities.csv",  # подкаталог
    "browser_profile/Default/Cookies",
])
def test_sensitive_files_answer_404_and_are_never_served(name):
    """Секреты из data/ наружу не уходят, и ответ — 404 (а не 403): он не должен
    подтверждать, что файл существует.

    Через `do_GET`, а не через приватный `_static_allowed()`: гард стоит В ЦЕПОЧКЕ
    `_serve_static` перед gzip-веткой, и проверка предиката напрямую оставила бы восемь
    «секретных» случаев зелёными при любой перестановке в цепочке (аудит 09.08.2026)."""
    assert _get(f"/{name}") == Resp(404, b"not found")


@pytest.mark.parametrize("name", [
    "feed.html", "feed.css", "feed.js", "feed-data.js", "feed-desc.js",
    "dashboard.html", "dashboard.css", "dashboard.js", "plotly-2.35.2.min.js",
])
def test_feed_and_dashboard_assets_are_served(name, tmp_path):
    """Всё, что подключают templates/feed.html.j2 и templates/dashboard.html.j2, доходит
    до клиента: 200 и СОДЕРЖИМОЕ файла (gzip-ветка, `Accept-Encoding: gzip`).

    Позитивная сторона белого списка тоже проверяется сквозняком: до 09.08.2026 её
    закрывал только `_static_allowed() is True`, то есть раздача не была проверена ни разу."""
    (tmp_path / name).write_text(f"/* {name} */", encoding="utf-8")
    r = _get(f"/{name}", directory=str(tmp_path), headers={"Accept-Encoding": "gzip"})
    assert r.status == 200
    assert gzip.decompress(r.body) == f"/* {name} */".encode()


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


# ────── работодатель в теле POST /api/apply (инцидент 01.08.2026) ──────
# Работодатель фиксируется В МОМЕНТ КЛИКА: к дренажу очереди вакансия уходит из выдачи,
# и карточка-призрак ленты не находится ни поиском по компании, ни воронкой автоотказов.
# Поле необязательное — клиент, который его не шлёт, получает прежнее поведение.


class _WorkerSpy:
    """Заглушка тёплого воркера откликов: запоминает аргументы submit, браузер не поднимает."""

    def __init__(self, status):
        self.calls = []
        self._status = status

    def submit(self, vid, url, cover, name="", employer=""):
        self.calls.append((vid, url, cover, name, employer))
        return {"status": self._status, "letter": False}


def _apply(monkeypatch, body, status="applied"):
    """POST /api/apply с телом `body` -> (Resp, шпион воркера, аргументы store.enqueue)."""
    from hrwork.application.apply import autoclick
    worker = _WorkerSpy(status)
    enqueued = []

    def _enqueue(*args):
        enqueued.append(args)
        return 3                                  # позиция в очереди ожидания

    monkeypatch.setattr(autoclick, "get_apply_worker", lambda: worker)
    monkeypatch.setattr(server.store, "enqueue", _enqueue)
    raw = json.dumps(body).encode("utf-8")
    h = _bare_handler(command="POST", headers={"Content-Length": str(len(raw))}, body=raw)
    return h._apply_post(), worker, enqueued


def test_apply_sends_the_employer_to_the_apply_worker(monkeypatch):
    _resp, worker, _q = _apply(monkeypatch, {"id": "77", "url": "https://hh.ru/vacancy/77",
                                             "cover": "письмо", "name": "Python Developer",
                                             "employer": "Acme"})
    assert worker.calls == [("77", "https://hh.ru/vacancy/77", "письмо",
                             "Python Developer", "Acme")]


def test_apply_without_employer_sends_empty_string(monkeypatch):
    # Обратная совместимость: старая лента шлёт тело без ключа employer.
    _resp, worker, _q = _apply(monkeypatch, {"id": "77", "url": "u", "cover": "c", "name": "n"})
    assert worker.calls == [("77", "u", "c", "n", "")]


def test_busy_apply_queues_the_employer_for_the_cron(monkeypatch):
    # Браузер занят кроном -> вакансия уходит в очередь ожидания, и работодатель обязан
    # уехать туда же: к моменту дренажа спросить его будет уже не у кого.
    resp, _worker, enqueued = _apply(monkeypatch, {"id": "77", "url": "u", "cover": "c",
                                                   "name": "n", "employer": "Acme"},
                                     status="busy")
    assert enqueued == [("77", "u", "n", "c", "Acme")]
    assert json.loads(resp.body) == {"status": "queued", "position": 3, "letter": False}


@pytest.mark.parametrize("raw, expected", [
    ("\u041e\u041e\u041e\x07\u0420\u043e\u043c\u0430\u0448\u043a\u0430\n", "ООО Ромашка"),   # C0-символ ломает построчный разбор журнала
    ("\u202eAcme", "Acme"),                    # bidi-переворот показа названия в ленте
    ("  Acme  ", "Acme"),
    ("Ц" * 250, "Ц" * 200),                      # потолок: запись журнала остаётся короткой строкой
    ({"name": "Acme"}, ""),                      # чужие данные: не-строка -> пусто, без падения
    (None, ""),
])
def test_employer_from_request_body_is_sanitized(raw, expected, monkeypatch):
    _resp, worker, _q = _apply(monkeypatch, {"id": "77", "employer": raw})
    assert worker.calls[0][4] == expected


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
    # Два раздельных утверждения с ТОЧНЫМ значением вместо цепочки `a and b`: подстрока
    # проходила и тогда, когда в contact уезжал весь текст сообщения («Звоните: …»),
    # а при падении цепочки не было видно, чата нет или контакт не тот (аудит 09.08.2026).
    assert set(data) == {"1"}                  # kind=none без контакта по-прежнему отсечён
    assert data["1"]["contact"] == "+7 912 345-67-89"
