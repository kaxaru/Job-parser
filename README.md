# HH Job Market Analyzer

Агрегатор IT-вакансий (HH.ru + hirify.me + talanto.work): сбор -> аналитика -> интерактивный
дашборд и лента-CRM с фильтрами под резюме и автооткликами через Playwright.

Локальная однопользовательская система на Windows. Хранилище — JSON-файлы;
PostgreSQL нужен только опциональному полнотекстовому поиску.

В составе репозитория — портфолио-подпроект [`dwh_demo/`](dwh_demo/README.md):
ETL (Ports & Adapters) в три хранилища **PostgreSQL / ClickHouse / MS SQL** -> BI в Metabase
-> оркестрация Airflow -> observability Grafana + Loki.

## Возможности

- **Сбор** — HH.ru (34 города × ~35 запросов, публичные HTML-страницы: API закрыт
  DDoS-Guard), hirify.me и talanto.work (JSON-API). Порталы собираются параллельно, дедуп по id.
- **Аналитика** — зарплаты по языкам / опыту / городам, топ-стеки, доля удалёнки -> CSV.
- **Свежесть вакансий** — возраст по `creationTime` vs `publicationTime`: отделяет свежие
  (<=30 дн) от гост-вакансий (>60 дн, месяцами переоткрываемых). Бейдж «👻 82д · переопубл.»
  в ленте, отсев гостов в автоклике.
- **Дашборд** — 14 вкладок Plotly + срезы по каждому порталу (`data/dashboard.html`).
- **Лента-CRM** — все вакансии карточками: фильтр «по резюме», бейджи совпадения и свежести,
  **реальный статус отклика с HH** (отказ / приглашение / интервью), сопроводительные письма,
  поиск, режим «Мои отклики за период» (`data/feed.html`).
- **Классификация переписки** — бейджи «кто ответил»: 👤 человек / 📋 шаблонная рассылка /
  🤖 бот, фильтр «ждут ответа». Практический эффект: 600 чатов -> 23 реальных дела.
- **Автоотклики** — по расписанию, до 200/сутки, трёхуровневый отбор кандидатов.
- **Отклик из ленты** — кнопка «🚀 Откликнуться в фоне» под `hh.py serve`: сервер жмёт
  «Откликнуться» через Playwright и шлёт письмо сообщением в чат (прямой fetch на HH
  из браузера невозможен — CORS + фингерпринт Group-IB).

## Установка

```bash
pip install -r requirements.txt           # рантайм
pip install -r requirements-dev.txt       # + pytest, ruff
python -m playwright install chromium     # для автокликов
```

JS-тулинг (опционально, для пересборки ленты) — standalone-бинари, без npm-зависимостей:
`esbuild` (сборка), `biome` (линтер), `node` >= 21 (тесты `node:test`).

Секреты — `.env` в корне (шаблон: `cp .env.example .env`): `HH_EMAIL`, `HH_PASSWORD`,
при необходимости `ANTHROPIC_API_KEY` (LLM-письма), `OPEN_ROUTER_API_KEY` + `INTENT_LLM=1`
(LLM-классификатор намерения в чатах, opt-in) и `PG*`. Полный список переменных —
[`docs/config.md`](docs/config.md).

### Профиль резюме — обязательный шаг

В репозитории лежит обезличенный шаблон. Скопируйте его и заполните своими данными:

```bash
cp resume_profile.example.json resume_profile.json   # Windows: copy
```

`resume_profile.json` — **в `.gitignore`**: внутри гражданство, вуз, стаж и факты для
ответов рекрутерам, этому не место в репозитории. Шаблон остаётся под контролем версий,
чтобы структура не потерялась.

Что заполнять:

- **`core`** — технологии-ядро, хотя бы одна должна быть в вакансии. Единый источник для
  автоклика и скоринга ленты
- **`exp_ids`** — допустимые уровни опыта: `noExperience`, `between1And3`, `between3And6`,
  `moreThan6`
- **`answers`** — факты для шаблонных ответов бот-рекрутерам. Заполнять строго по резюме:
  движок отвечает только отсюда и при отсутствии факта молчит, отдавая вопрос человеку

Без файла система стартует на дефолтах из `config.py` (`Python`/`FastAPI`, опыт до 3 лет),
а шаблонные ответы в чатах не работают — фактов нет.

## Первый запуск с нуля (свежий клон)

**Docker НЕ нужен для ядра.** Лента, сбор, автоклик, дашборд, аналитика работают на
JSON-хранилище без единого контейнера. Docker нужен ТОЛЬКО опциональным частям: серверному
поиску `/api/search` (PostgreSQL) и портфолио-подпроекту [`dwh_demo/`](dwh_demo/README.md).

После клона каталоги `data/`, `logs/`, `personal/` и файлы `resume_profile.json`, `.env`
отсутствуют (они в `.gitignore`) — их создаёт код или вы сами. Минимальный путь до рабочей
ленты:

```bash
# 1. окружение
python -m venv .venv3 && .venv3\Scripts\activate     # Windows (bash: source .venv3/Scripts/activate)
pip install -r requirements.txt
python -m playwright install chromium

# 2. секреты: скопировать шаблон и заполнить (минимум — HH_EMAIL / HH_PASSWORD)
copy .env.example .env                               # bash: cp .env.example .env

# 3. профиль резюме (иначе шаблонные ответы в чатах молчат)
copy resume_profile.example.json resume_profile.json   # bash: cp

# 4. разовый вход (капча руками в окне) — сессия ляжет в data/browser_profile/
python hh.py autoclick --login

# 5. первый сбор (data/ пуст после клона) + сборка ленты
python hh.py collect
python hh.py analyze
python hh.py feed
python hh.py serve                                   # http://127.0.0.1:8000/
```

Всё, что дальше про Docker (поиск, dwh_demo), — опционально и поверх этого.

## Запуск

```bash
python hh.py collect [--force]      # собрать вакансии -> data/vacancies_raw.json
python hh.py enrich [--only-empty]  # догрузить карточки
python hh.py analyze                # отчёты -> data/reports/*.csv
python hh.py dashboard              # -> data/dashboard.html
python hh.py feed                   # -> data/feed.html
python hh.py serve [--port N]       # сервер: лента + отметки + /api/apply + поиск
python hh.py autoclick [флаги]      # Playwright: поднятие резюме + автоотклики
python hh.py [all] [--force]        # collect + analyze
```

> ⚠ `--force` обходит санити-гейт, защищающий кеш от затирания деградированным срезом.
> Только руками и только когда точно известно, что спад реальный — см.
> [`docs/collect.md`](docs/collect.md).

## Автокликер

```bash
python hh.py autoclick --login                    # разово: вход с окном, капча руками
python hh.py autoclick --apply-limit 1 --headed   # один отклик, глазами
python hh.py autoclick --sync-status              # статусы и чаты, без браузера
python hh.py autoclick --bump-only                # только поднять резюме
```

**Капча.** HH на логине почти всегда показывает «Подтвердите, что вы не робот» — проходится
руками один раз в окне `--login`, это не автоматизируется by design. Дальше сессия живёт
в `data/browser_profile/` (persistent-профиль Chromium), и крон идёт headless неделями,
пока сессия не истечёт.

**Флаги:** `--apply-limit N` (за запуск, по умолчанию 10) · `--daily-cap N` (в сутки, 200 —
лимит HH) · `--cover template|llm` · `--sync-status` · `--headed` ·
`--bump-only` / `--apply-only`.

Эффективный лимит запуска = `min(apply-limit, дневной остаток)`, счётчик в
`data/apply_quota.json` сбрасывается по дате. Так N мелких запусков за день суммарно
не превысят cap.

**Сопроводительное письмо** уходит **сообщением в чат** после отклика (`chatik.hh.ru`,
cookie-only), а не в модалку: у чата нет фингерпринт-гейта Group-IB. Шаблоны —
`hrwork/application/apply/cover.py` и `src/feed/cover.js`.

Механика отбора кандидатов, watchdog, lock и квота — [`docs/apply.md`](docs/apply.md).

## Серверный поиск (PostgreSQL full-text)

Прод-путь «строка поиска -> backend -> PG `tsvector` -> ранжированная выдача», в отличие
от ленты с клиентским фильтром.

```bash
cd dwh_demo && docker compose up -d hh-postgres   # PG на порту 5433 (сервис hh-postgres)
python dwh_demo/search_demo/load.py               # вакансии + tsvector/GIN-индексы
python hh.py serve                                # http://127.0.0.1:8000/search
```

БД недоступна -> `503` только на `/api/search`, лента и отметки работают. Почему `tsvector`,
а не Elasticsearch — измерения в [`dwh_demo/search_demo/`](dwh_demo/search_demo/README.md).

## Опциональная настройка

Всё ниже — поверх рабочего ядра, каждый слой независим и по умолчанию выключен.

- **LLM-фичи (opt-in, по умолчанию OFF).** В `.env`: `OPEN_ROUTER_API_KEY` + флаг включения —
  `INTENT_LLM=1` (классификатор намерения в чатах), `REPHRASE_LLM=1` (переформулировка
  одобренного факта), `FORMS_LLM=1` (авто-заполнение форм-анкет). Для писем через LLM —
  `ANTHROPIC_API_KEY` + `--cover llm`. Без ключа/флага сеть не трогается, движок на regex +
  дословных фактах. Детали и границы приватности — [`docs/config.md`](docs/config.md),
  [`docs/security.md`](docs/security.md).
- **Серверный поиск (PostgreSQL FTS)** — см. раздел [«Серверный поиск»](#серверный-поиск-postgresql-full-text)
  выше: `docker compose up -d hh-postgres` + `search_demo/load.py`. Нужен только `/api/search`.
- **Крон (планировщик Windows)** — 5 задач (сбор, отклики, чаты, синк, поднятие резюме).
  На чистой машине их надо зарегистрировать: команды `schtasks /Create` и расписание —
  [`docs/operations.md`](docs/operations.md). После сбора крон опционально пересобирает
  поиск/дашборд/DWH (best-effort, требует поднятого Docker).
- **Портфолио-подпроект `dwh_demo`** — ETL в PostgreSQL/ClickHouse/MS SQL + Metabase +
  Airflow + Grafana/Loki. Полностью автономный, свой стек и инструкция:
  [`dwh_demo/README.md`](dwh_demo/README.md) (`cd dwh_demo && docker compose up -d`).
- **JS-тулинг** — только для пересборки фронта ленты: `esbuild`/`biome`/`node` (см.
  [«Установка»](#установка)). Без него `data/feed.js` берётся готовым из репо.

## Документация

Подробности по каждой подсистеме — в [`docs/`](docs/):

- [`architecture.md`](docs/architecture.md) — слои, поток данных, ключевые решения.
  **Начинать отсюда**
- [`domain.md`](docs/domain.md) — `Vacancy`, Value Objects, классификация, не-IT фильтр
- [`collect.md`](docs/collect.md) — источники, ACL, сеть, санити-гейт
- [`storage.md`](docs/storage.md) — файлы состояния, репозиторий, кеш описаний
- [`apply.md`](docs/apply.md) — тиры, отбор, watchdog, lock, квота
- [`chat.md`](docs/chat.md) — переписка, детект ботов, шаблонные ответы
- [`feed.md`](docs/feed.md) — лента (MVVM) и дашборд
- [`api.md`](docs/api.md) — HTTP-роуты локального сервера
- [`config.md`](docs/config.md) — константы и переменные окружения
- [`operations.md`](docs/operations.md) — крон, логи, диагностика
- [`errors.md`](docs/errors.md) — отказоустойчивость + каталог реальных инцидентов
- [`security.md`](docs/security.md) — секреты, XSS, анти-бот, prompt injection
- [`testing.md`](docs/testing.md) — тесты и критерии приёмки
- [`nfr.md`](docs/nfr.md) — нефункциональные требования
- `docs/history/` — внутренний архив закрытых аудитов (обоснования решений, цепочка
  «симптом -> причина -> фикс»). Локальный, в `.gitignore` — в репозиторий не входит.

Шаблоны: [`spec-template.md`](docs/spec-template.md) — техническая спека для обычной фичи;
[`rfc-template.md`](docs/rfc-template.md) — для крупного или необратимого (альтернативы,
миграция, откат).

Правила работы с кодом для ИИ-агентов — [`CLAUDE.md`](CLAUDE.md).

## Структура

```
hr_work/
├─ hh.py                 CLI: collect/enrich/analyze/dashboard/feed/serve/autoclick
├─ hrwork/
│  ├─ config.py          все константы и env
│  ├─ domain/            ядро: Vacancy, VO, парсинг и классификация
│  │  └─ models · salary · experience · schedule · role · freshness · parsing
│  ├─ application/
│  │  ├─ analyzer.py     агрегация статистики
│  │  └─ apply/          автоотклики (раскрой по контурам)
│  │     ├─ autoclick · candidates · cover · outcome · session   ядро отклика
│  │     ├─ chat/        chat · chat_class · chat_answer · chat_reply · chat_intent · chat_rephrase
│  │     ├─ forms/       forms · form_fill · form_read · form_status
│  │     └─ runtime/     store · lock · quota · bump_state
│  ├─ infrastructure/
│  │  ├─ sources/        hh · hirify · talanto · base (реестр порталов)
│  │  ├─ net/            http · proxy · rates
│  │  ├─ storage/        repository · jsonio · files · marks · followup
│  │  └─ search.py       Postgres FTS для /api/search
│  └─ presentation/
│     ├─ server.py       локальный HTTP-сервер
│     └─ views/          feed · dashboard · charts · reporter
├─ src/
│  ├─ feed/              MVVM ES-модули (esbuild -> data/feed.js)
│  │  └─ model · view · store · marks · resume · cover · main
│  ├─ dashboard.js       статика дашборда (копируется как есть)
│  └─ search.html        страница поиска
├─ templates/            Jinja: feed.css/html, dashboard.css/html (.j2)
├─ tests/                ~705 pytest + 84 node:test
├─ scripts/              chat_stats · check_proxies · peek · visualize
├─ docs/                 документация
│  └─ history/           архив закрытых аудитов (локальный, в .gitignore)
├─ cron/                 обёртки для планировщика задач Windows (*.bat + run_hidden.vbs)
├─ observability/        свой стек Loki+Promtail+Grafana для мониторинга крон-логов
├─ logs/                 ВСЕ логи: hr_work_<PID>.log + cron_*.log (в .gitignore)
├─ data/                 состояние и артефакты сборки (в .gitignore, создаётся кодом)
├─ personal/             резюме, письма, заметки к собеседованиям (в .gitignore)
├─ dwh_demo/             портфолио-подпроект (свой README, ruff, тесты)
├─ resume_profile.json   профиль: ядро стека, опыт, факты для ответов в чатах
├─ proxie.txt            ШАБЛОН прокси (реальные — proxie.bak.txt, в .gitignore)
├─ ruff.toml · biome.json · package.json · pytest.ini · conftest.py
└─ requirements.txt · requirements-dev.txt
```

## Разработка

```bash
pytest -q                    # Python
npm test                     # JS: node --test tests/feed/**/*.test.js
python -m ruff check .       # линтер Python (dwh_demo линтится отдельно)
npm run lint                 # biome, src/**
npm run build                # esbuild -> data/feed.js (или просто python hh.py feed)
```

`python hh.py feed` пересобирает бандл сам (нужен `esbuild` в PATH). У `dwh_demo/` свой
`ruff.toml` и `pytest.ini` — оба конфига применяются к своим файлам.

Правка фронта: `src/feed/*.js` -> `python hh.py feed`. Проверка: `npm run lint` +
`npm test` + `node --check data/feed.js`.

## Под своё резюме

**Ядро стека и опыт** — `resume_profile.json` в корне (копия
`resume_profile.example.json`, см. [«Профиль резюме»](#профиль-резюме--обязательный-шаг)).
Это **единый источник** для autoclick-фильтра и скоринга ленты: Python читает его
в `config.RESUME_CORE` / `RESUME_EXP_IDS`, а `feed.py` инжектит в `feed-data.js`.
Раньше константы дублировались в `resume.js` и молча разошлись.

Там же блок `answers` — факты для шаблонных ответов бот-рекрутерам.

Правя структуру профиля, обновляйте и шаблон: `resume_profile.example.json` — то, что
увидит следующий пользователь, и разошедшийся шаблон хуже отсутствующего.

**Веса бейджа «% совпадения»** — `src/feed/resume.js`: `RESUME_WEIGHTS`, `EXP_SCORE`
(стек 60 + опыт 25 + удалёнка 15), `RESUME_REQUIRE_REMOTE`.

**Тексты писем** — `src/feed/cover.js`: `COVER_TEMPLATES`, `RESUME_TECH_SET`, `TECH_LABEL`.

**Тиры 2–3 автоклика** — `config.py`: `APPLY_CORE_WIDE`, `APPLY_OFFICE_CITIES`.
Известный компромисс: лента о них не знает и покажет «не матч» вакансиям, на которые
крон реально откликнется.

После правок: `npm test` -> `python hh.py feed`.

## Заметки

- **Отметки** откликов и отказов живут в `data/marks.json` и переживают любой пересбор —
  сбор этот файл не трогает.
- **Селекторы** — только `data-qa` и текстовые локаторы: хэшированные magritte-классы HH
  меняются каждым деплоем.
- **Поднятие резюме** — только бесплатная кнопка `resume-update-button` с гардом по тексту
  «в поиске»; платную «Поднять автоматически» скрипт не трогает.
- **DDoS-Guard** иногда отдаёт headless деградированную страницу: `_goto` переживает
  челлендж за 3 попытки, `bump_resumes` при неудаче возвращает `-1` и пишет в лог, а не
  молча «0». Если headless стабильно упирается — добавить `--headed`.
- **dwh_demo** — отдельный Docker-стек из 12 сервисов со своим README:
  `cd dwh_demo && docker compose up -d`.
