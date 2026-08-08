# HTTP API локального сервера

## Overview

Сервер ленты: `python hh.py serve [--port N]`. Stdlib `http.server`
(`ThreadingHTTPServer`), bind **`127.0.0.1`**, порт `SERVE_PORT` (8000).
Аутентификации нет — см. [`security.md`](security.md).

```
Browser (лента / страница поиска)
   │
   ▼
ThreadingHTTPServer ──── run_server()
   │
   ▼
_Handler.do_GET / do_POST
   │  маршрут из таблицы _GET_ROUTES
   ▼
прикладной хендлер ──> Resp(status, body, ctype, headers)
   │
   ├─> store (ApplicationStore) ────> data/*.json          отметки, статусы, журнал
   ├─> chat_class.analyze ──────────> свёртка переписки
   ├─> search.search ───────────────> PostgreSQL (tsvector)
   └─> get_apply_worker().submit ──> Playwright ──> Chromium ──> HH.ru
   │
   ▼
_Handler._write  ← единственный писатель в сокет
```

**Ответ — это значение.** Хендлеры возвращают `Resp` и не трогают сокет; пишет ровно один
`_write`. Развязывает «что отвечаем» от «как пишем» и делает хендлеры тестируемыми —
`tests/backend/presentation/test_server.py` дёргает их напрямую, без сокета и без порта.

Маршрутизация — таблица `_GET_ROUTES` (путь -> метод), определённая после класса,
а не цепочка `if`.

## Соглашения

- Все JSON-ответы: `application/json; charset=utf-8`, `Cache-Control: no-store`,
  `ensure_ascii=False` (кириллица не экранируется)
- Тело POST: не более **1 МБ** (`_MAX_BODY`), иначе `413`
- Путь нормализуется до query-строки и без хвостового `/`
- Статика из `data/`: белый список имён (`_ALLOWED_STATIC`), gzip + ETag для текстовых
  типов, `304` при совпадении `If-None-Match`
- Ошибки логируются только для кодов >= 400 (`log_request`)

### Гарды источника запроса (08.08.2026)

Сервер слушает `127.0.0.1`, но браузер владельца ходит в интернет — и страница из соседней
вкладки умеет слать сюда запросы. Два гарда закрывают этот вектор, подробности и модель
угроз — в [`security.md`](security.md).

- **Любой запрос:** `Host` обязан быть петлевым именем (`_Handler::_host_ok`,
  `_LOCAL_HOSTS` = `127.0.0.1` | `localhost` | `::1`). Чужой `Host` -> **`421`**
  `misdirected request`. Это защита от DNS-rebinding: адрес сокета от неё не спасает,
  потому что после ребиндинга страница атакующего становится same-origin и может **читать**
  ответы, а не только слать запросы.
- **Оба POST:** `Sec-Fetch-Site` = `same-origin` либо `none`, а если пришёл `Origin` — его
  хост тоже петлевой (`_Handler::_same_origin`). Иначе **`403`** `cross-origin POST rejected`
  плюс `WARNING` в лог с обоими заголовками.

Не-браузерный клиент (curl, скрипт) не шлёт ни `Sec-Fetch-Site`, ни `Origin` и проходит —
это осознанно: «любой процесс на машине» остаётся принятым риском, гарды закрывают именно
браузерный вектор.

---

## Routes

### GET /api/marks

**Назначение.** Отметки пользователя: что отмечено как отклик, что как отказ.
Источник правды на диске, переживает пересбор данных.

**Request**

```
GET /api/marks
```

**Response** `200`

```json
{
  "80675242": "rejected",
  "92354865": "rejected",
  "134662537": "applied"
}
```

Ключ — id вакансии, значение — `applied` | `rejected`.

**Status Codes.** `200`

**Implementation.** `_Handler._marks_get` -> `store.marks()`

**Edge cases**

- Файла нет или битый JSON -> `{}` (не ошибка)
- Значения вне allowlist отфильтровываются при чтении

---

### POST /api/marks

**Назначение.** Полная перезапись отметок. Лента шлёт весь словарь с debounce 400 мс.

**Request**

```
POST /api/marks
Content-Type: application/json

{"134662537": "applied", "80675242": "rejected"}
```

**Response** `204 No Content`, тело пустое.

**Status Codes**

- `204` — записано
- `400` — `bad content-length` | `bad json` | `expected object`
- `413` — `payload too large` (> 1 МБ)

**Implementation.** `_Handler._marks_post` -> `store.set_marks()`

**Edge cases**

- Тело не объект (список, строка) -> `400`, файл не трогается
- Пустое тело -> трактуется как `{}` -> **отметки будут стёрты**
- Значения фильтруются по allowlist при записи: чужой статус на диск не попадёт

**Почему так.** Полная перезапись, а не патч: словарь мал (единицы КБ), а инкрементальные
обновления потребовали бы разрешения конфликтов между вкладками. Запись атомарная и под
`threading.Lock` — сервер многопоточный.

---

### GET /api/forms

**Назначение.** Форм-очередь: вакансии-опросники, которые автоклик не заполняет.
Лента рисует по ним бейдж «форма» без пересборки.

**Request**

```
GET /api/forms
```

**Response** `200`

```json
{
  "134427427": {
    "name": "Python-backend developer",
    "url": "https://hh.ru/vacancy/134427427",
    "ts": ""
  }
}
```

**Status Codes.** `200`

**Implementation.** `_Handler._forms_get` -> `store.forms()`

**Edge cases**

- `ts` пустая строка для записей, добавленных до введения таймстампа
- Запись идемпотентна по id: повторный опросник не дублируется

**Почему так.** Опросники исключаются из отбора через отдельную очередь, а не через `marks` —
чтобы в ленте они не выглядели отказом и чтобы фильтр снимался одним флагом
`APPLY_SKIP_FORMS`.

---

### GET /api/statuses

**Назначение.** Реальные статусы откликов с HH, собранные синком. CRM-бейджи в ленте.

**Request**

```
GET /api/statuses
```

**Response** `200`

```json
{
  "134662537": "RESPONSE",
  "133629751": "DISCARD"
}
```

Значение — код `currentApplicantState` от HH. Подписи — `chat.STATE_LABELS`
(`RESPONSE` -> «Отклик», `DISCARD` / `DISCARD_BY_EMPLOYER` -> «Отказ», `INVITATION` ->
«Приглашение»). Незнакомый код показывается как есть.

**Status Codes.** `200`

**Implementation.** `_Handler._statuses_get` -> `store.statuses()`

**Edge cases**

- Наполняется только `autoclick --sync-status`; без синка данные устаревают молча
- Новый код статуса от HH не ломает ленту — рендерится сырым

---

### GET /api/applied

**Назначение.** Журнал откликов с таймстампами — для режима «Мои отклики за период».

**Request**

```
GET /api/applied
```

**Response** `200`

```json
[
  {
    "id": "134809303",
    "name": "BI-аналитик",
    "url": "https://hh.ru/vacancy/134809303",
    "via": "cron",
    "status": "applied",
    "ts": "2026-07-05T03:27:34"
  }
]
```

`via` — `cron` | `feed` | `hh` (`ApplyChannel`). `hh` означает отклик, сделанный руками
на сайте и подхваченный синком из чатов.

**Status Codes.** `200`

**Implementation.** `_Handler._applied_get` -> `store.applied_log()`

**Edge cases**

- Массив, не словарь: одна вакансия может встречаться несколько раз (крон + дожурналирование).
  Лента берёт **самый ранний** `ts`
- Битые строки JSONL пропускаются при чтении, остальной журнал читается

---

### GET /api/chats

**Назначение.** Свёртка переписки по всем чатам: что ответил работодатель, ждёт ли он
ответа и кто именно написал.

**Request**

```
GET /api/chats
```

**Response** `200`

```json
{
  "134020511": {
    "kind": "other",
    "label": "💬 сообщение",
    "needs_reply": true,
    "manual_only": false,
    "sender": "human",
    "can_write": true,
    "preview": "Здравствуйте, Антон, Компания Ozon Tech рассмотрит Ваше резюме…",
    "ts": "2026-07-14T09:12:03"
  }
}
```

**Поля**

- `kind` — `question` | `screening` | `invite` | `reject` | `other`
  (`none` отфильтровывается и в ответ не попадает)
- `label` — готовая подпись с эмодзи для бейджа
- `needs_reply` — `false` только для `reject`
- `manual_only` — вопрос про деньги или переезд, отвечать должен человек
- `sender` — `bot` | `template` | `human` (см. ниже)
- `can_write` — можно ли вообще написать в этот чат
- `preview` — первые 160 символов последнего сообщения
- `ts` — время последнего сообщения

**Status Codes.** `200`

**Implementation.** `_Handler._chats_get` -> `store.chat_messages()` ->
`chat_class.build_template_index` -> `chat_class.analyze`

**Edge cases**

- Чаты с `kind == "none"` (пусто либо последнее слово за нами) в ответ **не включаются**
- `can_write: false` — чат закрыт на запись; выясняется заранее из `writePossibility`,
  а не по `409 CHAT_DOES_NOT_EXIST` при отправке
- Индекс шаблонов строится **по всему корпусу** на каждый запрос: рассылку от имени живого
  рекрутера иначе не отличить от личного письма

**Почему так.** `sender` — два независимых сигнала: `isBot` из API ловит ботов HH, но
пропускает шаблоны от имени человека; повтор текста по корпусу ловит рассылку, но пропускает
шаблон, отправленный впервые. Ошибаться безопаснее в сторону «личное». Детали —
[`chat.md`](chat.md).

Текущий срез: 281 чат в ответе, из них `sender` распределён по всем трём значениям.

---

### POST /api/apply

**Назначение.** Отклик на вакансию в фоне через Playwright. Кнопка «🚀 Откликнуться
в фоне» в модалке ленты.

> ⚠ Отправляет **реальный отклик** работодателю. Необратимо.

**Request**

```
POST /api/apply
Content-Type: application/json

{
  "id": "134809303",
  "url": "https://hh.ru/vacancy/134809303",
  "name": "Python Developer",
  "employer": "Acme",
  "cover": "Здравствуйте! Заинтересовала вакансия…"
}
```

**Parameters** (тело JSON)

- `id` — string, **required**; пустой или отсутствует -> `400`
- `url` — string, опционально
- `name` — string, опционально
- `employer` — string, опционально, по умолчанию `""`; работодатель карточки. Фиксируется
  **в момент клика** и уезжает в `applied_log.jsonl`: к моменту дренажа очереди вакансия
  уже выпадает из выдачи, и карточка-призрак ленты без работодателя не ищется ни по
  компании, ни воронкой автоотказов (инцидент 01.08.2026). Тело без ключа принимается
  как раньше — обратная совместимость сохранена
- `cover` — string, опционально; текст сопроводительного письма

`employer` нормализуется `server.py::_clean_line`: не-строка (число, объект, `null`) -> `""`;
управляющие символы (C0, DEL, C1 и bidi-переопределения `U+202A..U+202E`, `U+2066..U+2069`)
заменяются **пробелом**, а не выкусываются («ООО\nРомашка» -> «ООО Ромашка», не
«ОООРомашка»); пробелы схлопываются; длина режется до **200** символов (`_MAX_EMPLOYER`).
Ограничение не косметическое: целостность `applied_log.jsonl` держится на том, что запись
остаётся одной короткой строкой одного `write` (`followup.py::append_applied`).

**Response** `200` — успешный отклик

```json
{"status": "applied", "letter": true}
```

**Response** `200` — браузер занят кроном, вакансия поставлена в очередь

```json
{"status": "queued", "position": 2, "letter": false}
```

`status` — доменный исход `applied` | `already` | `form` | `skip` (`ApplyOutcome`) либо
транспортное состояние `queued` | `busy` | `no-session` | `error`.

**Status Codes**

- `200` — запрос обработан (проверять поле `status`, а не только код)
- `400` — `{"error": "bad json"}` | `{"error": "no id"}`
- `413` — тело больше 1 МБ
- `500` — `{"status": "error", "error": "TypeError: …"}`, трейс в лог сервера

**Implementation.** `_Handler._apply_post` -> `get_apply_worker().submit()` ->
`ApplyWorker` -> `autoclick.apply_vacancy`

**Edge cases**

- **Занят lock** (идёт крон-батч) -> `queued` + позиция в `apply_pending.json`; крон
  дожмёт очередь в конце своего прогона. Это не ошибка. `employer` кладётся в запись
  очереди (`store.enqueue`) и доживает до дренажа — иначе отложенный отклик терял бы
  компанию ровно там, где она нужнее всего
- **Протухла сессия** -> `no-session`, нужен `hh.py autoclick --login`
- **Вакансия-опросник** -> `form`, попадает в форм-очередь
- **Уже откликались** -> `already`
- **Параллельные POST** -> один воркер-синглтон (double-checked locking); два Chromium
  на persistent-профиль сломали бы профиль
- **Долгий ответ** — отклик идёт через реальный браузер и DDoS-Guard, клиент ждёт;
  предохранитель `RESULT_TIMEOUT = 600 с`

**Почему так.** Прямой запрос на HH из браузера невозможен: CORS плюс фингерпринт Group-IB.
Тёплый воркер переиспользует один браузер между кликами и закрывается по простою
(`IDLE_TIMEOUT = 300 с`), чтобы не держать lock и не поднимать Playwright заново на каждый
отклик.

**Осознанный компромисс.** Нормализуется только `employer`; `name` и `url` уезжают в тот же
журнал как есть — это поведение существующих полей, и правка про работодателя его не меняла.
Если выравнивать, то правка на одну строку: те же `_clean_line(...)` на месте
`body.get("name", "")`.

---

### GET /api/search

**Назначение.** Полнотекстовый поиск через PostgreSQL — прод-путь «строка -> backend ->
tsvector -> ранжированная выдача», в отличие от ленты с клиентским фильтром.

**Производительность** (замер 01.08.2026, 87k строк; профилировали по жалобе «первый
запрос долгий»). Самый дорогой запрос — НЕ поиск, а стартовый показ страницы: `search.html`
на загрузке зовёт `newSearch()` без `q`, то есть выборку без единого WHERE по всей таблице.
Было 541 мс, стало 10 мс. Что сняли, по вкладам:

| правка | что было | выигрыш |
|---|---|---|
| `work_mem=64MB` на соединении | WindowAgg сваливал ~86 МБ в temp-файлы | 321 мс |
| `total` отдельным `count(*)` вместо `count(*) OVER()` | оконная функция тащила все совпадения с `doc`/`description` | 76 мс |
| индекс `ix_salmid_page (sal_mid DESC NULLS LAST, id)` | Seq Scan + Sort по 87k строк | 137 мс |
| пул соединений вместо коннекта на запрос | 18.4 мс на коннект при 9.8 мс на сами запросы | ~18 мс |

Кеша на стороне приложения нет и не появилось: ускорение повторного запроса, которое видно
глазом, — это buffer cache PostgreSQL (`shared_buffers=128MB` при таблице 232 МБ, так что
после простоя срез снова холодный).

**Request**

```
GET /api/search?q=python&city=Москва&sal=150000&limit=20&offset=0
```

**Parameters** (query)

- `q` — string; пусто -> режим PLAIN (без ранжирования, сортировка по зарплате)
- `city` — string
- `sal` — int; **в query именно `sal`**, внутри маппится в `sal_min`
- `fresh` — класс свежести из белого списка: `fresh` (≤30 дн) | `recent` (31–60) | `ghost` (>60);
  пороги из `domain.freshness` (`FRESH_DAYS`/`GHOST_DAYS`). Возраст — **полные сутки** от
  `now()` (`search.py::_AGE` = `floor(extract(epoch from now() - created_at) / 86400)`),
  то есть ровно то же число, что показывает лента. Раньше считалась календарная разница дат
  в таймзоне сессии Postgres, и на границах 30/31 и 60/61 класс расходился с лентой:
  созданная `2026-07-08T20:00Z` вакансия в `2026-08-08T06:00Z` была `fresh` в ленте и
  `recent` в поиске, то есть выпадала из `fresh=fresh`
- `source` — портал из белого списка `search.py::SOURCES`, а это **`tuple(config.SOURCES)`**,
  то есть все девять действующих порталов: `hh` | `hirify` | `talanto` | `getmatch` |
  `arbeitnow` | `himalayas` | `web3` | `themuse` | `jobicy`. Свой кортеж здесь держать нельзя —
  он уже разъезжался с реальным набором (getmatch, 01.08.2026), и новый портал молча выпадал
  из фильтра поиска, хотя в данных был
- `limit` — int, потолок **100** (`LIMIT_MAX`)
- `offset` — int

**Response** `200`

```json
{
  "query": "python",
  "city": "Москва",
  "sal_min": 150000,
  "fresh": "fresh",
  "source": "hh",
  "total": 412,
  "count": 20,
  "limit": 20,
  "offset": 0,
  "results": [
    {
      "id": "134809303",
      "source": "hh",
      "name": "Python Developer",
      "employer": "Ozon Tech",
      "city": "Москва",
      "sal_mid": 250000,
      "age_days": 10,
      "fresh": "fresh",
      "snippet": "опыт <mark>Python</mark> от 3 лет…",
      "rank": 0.0607
    }
  ]
}
```

**Response** `503` — БД недоступна

```json
{
  "error": "…",
  "hint": "подними hh-postgres и прогони dwh_demo/search_demo/load.py"
}
```

**Status Codes**

- `200` — выдача (возможно пустая)
- `503` — `SearchUnavailable`
- `500` — прочая ошибка, трейс в лог сервера

**Implementation.** `_Handler._search` -> `search.search()`; режим выбирает `_mode_for(q)`
(FTS через `websearch_to_tsquery('russian')` либо PLAIN), SQL собирает `_build_sql`.

**Edge cases**

- **БД недоступна** -> `503`; лента, отметки и отклики продолжают работать
- **`limit` > 100** -> обрезается до `LIMIT_MAX`
- **Пустой `q`** -> режим PLAIN: сниппет `left(description, 180)`, сортировка по `sal_mid`
- **`total`** считается **отдельным `count(*)`** (`_Mode::count_sql`) на том же WHERE, а не
  `count(*) OVER()` — см. таблицу производительности выше, минус 76 мс. Оба запроса идут
  в ОДНОЙ транзакции, поэтому total согласован со страницей

**Почему так.** Поиск отделён от ленты, чтобы отказ БД не влиял на просмотр вакансий —
это единственная подсистема с внешней зависимостью в рантайме. `psycopg2` импортируется
лениво внутри `_connect()`, чтобы сборка SQL тестировалась без установленного драйвера.

**Безопасность.** Все пользовательские входы — только параметры psycopg2. В текст SQL
подставляется единственная вещь: имя таблицы из `SEARCH_TABLE`, то есть значение под
контролем владельца машины.

`snippet` — единственное поле выдачи, которое страница поиска вставляет в DOM **без**
`esc()`: иначе не работала бы подсветка `<mark>` из `ts_headline`. Поэтому HTML описания
экранируется в SQL **до** `ts_headline` (`search.py::_ESC_DESC`), и в сниппете остаётся
только та разметка, которую сгенерировали мы сами. Детали — [`security.md`](security.md).

---

### GET /search, /search.html

**Назначение.** Страница поиска.

**Response** `200` — `text/html; charset=utf-8`, содержимое `src/search.html`.

**Status Codes.** `200` · `404` — файл отсутствует

**Implementation.** `_Handler._search_page`

**Особенность.** Отдаётся из `src/`, а не из `data/` — это исходник, не артефакт сборки.

---

### GET /* — статика

**Назначение.** Артефакты ленты и дашборда из `data/`; `/` -> `feed.html`.

**Белый список, а не весь каталог** (`_Handler::_static_allowed`, `_ALLOWED_STATIC`):
`feed.html`, `feed.css`, `feed.js`, `feed-data.js`, `feed-desc.js`, `dashboard.html`,
`dashboard.css`, `dashboard.js` плюс скачанный `_ensure_plotly` бандл по маске
`_ALLOWED_STATIC_RX` (`plotly-<версия>.min.js`). Только из корня `data/`: путь с `/`
внутри (`reports/`, `browser_profile/`) отбивается сразу. До 08.08.2026 каталог отдавался
целиком, и вместе с `feed.js` наружу смотрели куки сессии HH — см. [`security.md`](security.md).

**Response.** Для `.html`, `.js`, `.css`, `.json`, `.svg`, `.txt` при
`Accept-Encoding: gzip`:

```
200 OK
Content-Encoding: gzip
ETag: "1752998400-21413484"
Cache-Control: no-cache
```

Остальное стримится через `super().do_GET()`.

**Status Codes.** `200` · `304` (совпал `If-None-Match`) · `404`

**Implementation.** `_Handler._serve_static` -> `_static_allowed` -> `_gzip_resp` -> `_gzipped`

**Edge cases**

- Файл вне белого списка -> **`404`, а не `403`**: код ответа не должен подтверждать, что
  такой файл в `data/` есть
- Клиент без `Accept-Encoding: gzip` -> обычный стрим
- Path traversal отсекается `translate_path` из stdlib
- Кэш сжатого инвалидируется по `(mtime, size)`

**Почему так.** Без кэша каждый запрос пере-сжимал бы `feed-desc.js` — на срезе 08.08.2026
это 274 МБ. ETag с `no-cache` даёт `304` вместо повторной передачи, но заставляет браузер
перепроверять — артефакты пересобираются часто.

---

## Логирование

Стандартный access-лог заглушён (`log_message`). Через loguru проходят только коды >= 400
(`log_request`) — иначе лента с ленивой подгрузкой залила бы лог сотнями строк на сессию.

Исключения в `/api/apply` и `/api/search` логируются с трейсом через `log.exception`.

## Проверка

```bash
python hh.py serve --port 8000

curl -s http://127.0.0.1:8000/api/marks | head -c 200
curl -s http://127.0.0.1:8000/api/chats | head -c 300
curl -s "http://127.0.0.1:8000/api/search?q=python&limit=3"
curl -si http://127.0.0.1:8000/feed-data.js | grep -i "content-encoding\|etag"
curl -si -X POST http://127.0.0.1:8000/api/marks -d '[]'      # ожидаем 400

# гарды источника
curl -si http://127.0.0.1:8000/hh_state.json                            # ожидаем 404
curl -si -H "Host: evil.example" http://127.0.0.1:8000/api/marks        # ожидаем 421
curl -si -X POST -H "Sec-Fetch-Site: cross-site" \
     http://127.0.0.1:8000/api/marks -d '{}'                            # ожидаем 403
```

Ожидаемо: `marks` — JSON-словарь; `chats` — словарь свёрток; `search` — результаты либо
`503` без БД; статика — `content-encoding: gzip` и `etag`; POST с массивом — `400
expected object`. Гарды: куки сессии — `404`, чужой `Host` — `421 misdirected request`,
кросс-сайтовый POST — `403 cross-origin POST rejected` (плюс `WARNING` в логе сервера).
