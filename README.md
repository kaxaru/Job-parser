# HH Job Market Analyzer

[![CI](https://github.com/kaxaru/Job-parser/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/kaxaru/Job-parser/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1628%20py%20%2B%20129%20js-success)](docs/testing.md)
[![Ruff](https://img.shields.io/badge/lint-ruff%20%2B%20biome%20%2B%20mypy%20strict-informational)](ruff.toml)

Агрегатор IT-вакансий с **9 порталов** (hh.ru, hirify.me, talanto.work, getmatch.ru,
arbeitnow.com, himalayas.app, web3.career, themuse.com, jobicy.com): сбор -> аналитика ->
интерактивный дашборд и лента-CRM с фильтрами под резюме и автооткликами через Playwright.

Локальная однопользовательская система на Windows. Хранилище — JSON-файлы;
PostgreSQL нужен только опциональному полнотекстовому поиску.

В составе репозитория — портфолио-подпроект [`dwh_demo/`](dwh_demo/README.md):
ETL (Ports & Adapters) в три хранилища **PostgreSQL / ClickHouse / MS SQL** -> BI в Metabase
-> оркестрация Airflow -> observability Grafana + Loki.

## Как выглядит

Лента-CRM: карточки с бейджами совпадения и свежести, статусом отклика с HH, состоянием
переписки; фильтры сворачиваются в липкую полосу, тема переключается в шапке.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/img/feed-light.png">
  <img alt="Лента вакансий" src="docs/img/feed-dark.png">
</picture>

Дашборд: 15 вкладок Plotly со срезами по каждому порталу.

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/img/dashboard-light.png">
  <img alt="Дашборд рынка" src="docs/img/dashboard-dark.png">
</picture>

> Скриншоты следуют теме GitHub — светлая версия у обоих своя.
> **CRM-слой на кадрах сгенерирован**: отклики, отказы и переписка синтетические, реальная
> история откликов в репозиторий не попадает. Вакансии — обычная публичная выдача.

## Возможности

- **Сбор** — 9 порталов (`config.SOURCES`): HH.ru (34 города × ~35 запросов, публичные
  HTML-страницы: API закрыт DDoS-Guard) плюс JSON-API hirify.me, talanto.work, getmatch.ru,
  arbeitnow.com, himalayas.app, web3.career, themuse.com, jobicy.com. Порталы собираются
  параллельно, дедуп по id внутри портала и кросс-портальный (`domain/dedup.py`).
  В кеше 109 798 вакансий (`data/vacancies_raw.json`, ~494 МБ; замер 07.08.2026).
- **Аналитика** — зарплаты по языкам / опыту / городам, топ-стеки, доля удалёнки -> CSV.
- **Свежесть вакансий** — возраст по `creationTime` vs `publicationTime`: отделяет свежие
  (<=30 дн) от гост-вакансий (>60 дн, месяцами переоткрываемых). Бейдж «👻 82д · переопубл.»
  в ленте, отсев гостов в автоклике.
- **Дашборд** — 15 вкладок Plotly + срезы по каждому порталу (`data/dashboard.html`),
  включая воронку автоотказов.
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
pip install -r requirements-dev.txt       # + pytest, ruff, mypy, pre-commit
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

Остальные ключи (блеклист вакансий, тиры отбора, поисковые запросы и города, тексты писем,
ответы на анкеты) — необязательные, каждый со своим дефолтом. Разбор всех — в разделе
[«Под своё резюме»](#под-своё-резюме); пояснения к каждому ключу лежат прямо в шаблоне.

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
python hh.py chat [флаги]           # автоответы в переписке (без --send — только показать)
python hh.py forms [флаги]          # форм-очередь: --dry-run / --sweep / --clean / дренаж
python hh.py hhapi --login|--probe  # официальный API hh.ru: OAuth и проверка прав
python hh.py [all] [--force]        # collect + analyze
```

Всего 11 режимов (`hh.py::Mode`), диспетчеризация по таблице `_HANDLERS`.

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
- **Крон (планировщик Windows)** — 4 активные задачи (`hh_collect`, `hh_apply`, `hh_chat`,
  `hh_sync`); пятая, `hh_bump` (поднятие резюме), заведена, но **отключена**.
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
├─ hh.py                 CLI, 11 режимов: all/collect/enrich/analyze/dashboard/feed/
│                        serve/autoclick/chat/forms/hhapi
├─ hrwork/
│  ├─ config.py          все константы и env
│  ├─ domain/            ядро: Vacancy, VO, парсинг и классификация
│  │  └─ models · salary · experience · schedule · role · freshness · parsing · dedup
│  ├─ application/
│  │  ├─ analyzer.py     агрегация статистики
│  │  ├─ funnel.py       воронка автоотказов (латентность, срез по компаниям)
│  │  └─ apply/          автоотклики (раскрой по контурам)
│  │     ├─ autoclick · candidates · cover · outcome · session   ядро отклика
│  │     ├─ chat/        chat · chat_class · chat_answer · chat_reply · chat_intent · chat_rephrase
│  │     ├─ forms/       forms · form_fill · form_read · form_status
│  │     └─ runtime/     store · lock · quota · bump_state
│  ├─ infrastructure/
│  │  ├─ sources/        base (реестр) · hh · hh_api · hirify · talanto · getmatch ·
│  │  │                  arbeitnow · himalayas · web3career · themuse · jobicy
│  │  │                  __init__.py импортирует каждый модуль — этим он и регистрируется
│  │  ├─ net/            http · proxy · rates
│  │  ├─ storage/        repository · jsonio · files · marks · followup
│  │  ├─ llm/            openrouter (транспорт opt-in LLM-фич)
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
├─ tests/                1628 pytest (backend/) + 129 node:test (feed/)
├─ tools/                инфраструктура сборки, версионируется целиком: check_venv.py
│                        (гейт интерпретатора, первый хук pre-commit)
├─ scripts/              ЛОКАЛЬНАЯ отладочная песочница: в репо только chat_stats ·
│                        chat_review, остальные 4 (check_proxies · peek · visualize ·
│                        dbg_search) — в .gitignore
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
├─ .pre-commit-config.yaml   те же проверки, что в CI, до пуша (venv-гейт -> ruff ->
│                            mypy strict -> pytest -> npm test -> biome)
├─ ruff.toml · biome.json · package.json · pytest.ini · mypy.ini · conftest.py
└─ requirements.txt · requirements-dev.txt
```

## Разработка

```bash
pytest -q                    # Python: 1628 тестов (6 opt-in skip)
npm test                     # JS: node --test tests/feed/**/*.test.js — 129 тестов
python -m ruff check .       # линтер Python (dwh_demo линтится отдельно)
python -m mypy               # строгая проверка типов (hrwork + hh.py), ноль ошибок
npm run lint                 # biome, src/**
npm run build                # esbuild -> data/feed.js (или просто python hh.py feed)
```

`python hh.py feed` пересобирает бандл сам (нужен `esbuild` в PATH). У `dwh_demo/` свой
`ruff.toml` и `pytest.ini` — оба конфига применяются к своим файлам.

### Pre-commit

Всё то же самое, но до пуша — конфиг `.pre-commit-config.yaml`:

```bash
pre-commit install                   # один раз на клон (сам пакет — в requirements-dev.txt)
pre-commit run --all-files           # прогнать по всему репо
```

Порядок хуков: гейт venv (`tools/check_venv.py`) -> ruff -> mypy strict -> pytest ->
npm test -> biome. `language: system` — инструменты берутся из **активированного** окружения,
а не из отдельных venv'ов фреймворка, поэтому коммитить надо из активного `.venv3`.
Пропуск: `SKIP=pytest git commit ...` (один хук) или `git commit --no-verify` (все).
Подробности и причины — [`docs/testing.md`](docs/testing.md).

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

**Какие вакансии НЕ брать** — блок `blacklists` того же файла. Это предпочтения соискателя,
а не логика приложения: «не беру QA» и «беру только QA» — одинаково законные настройки,
поэтому править исходник не нужно. Девять ключей, значение — регекс по ТАЙТЛУ:

| ключ | что отсекает |
|---|---|
| `senior` | грейд выше вашего (senior/lead/ведущий/тимлид/principal/staff) |
| `management` | руководящие: руководитель, Head, Director, VP, C-level, архитектор |
| `non_engineering` | формально IT, но роль не инженерная (риск-аналитик, портфельный) |
| `qa` | тестирование |
| `analyst` | аналитики любые |
| `ml` | ML/DS (LLM и RAG сюда не входят) |
| `devops` | эксплуатация вместо разработки |
| `other_lang` | другой основной язык (не применяется, если в тайтле есть python) |
| `target_engineering` | **исключение, а не запрет**: снимает `analyst` и `ml` |

Правила проверяются в порядке таблицы, побеждает первое совпавшее. `qa` стоит до
`target_engineering` намеренно: «QA Engineer (LLM-платформа)» — это тестирование.

```jsonc
"blacklists": {
  "qa": "",                       // пусто = правило ВЫКЛЮЧЕНО, QA вам интересны
  "devops": "\\bsre\\b",          // сузить до одного SRE
  "senior": "\\bstaff\\b|\\bprincipal\\b"
}
```

Ключ отсутствует — берётся дефолт из `candidates.py::_rx`. Битый регекс не роняет сбор:
предупреждение в лог и дефолт, потому что тихо отключённый фильтр отбора хуже чужого.

**Кому и как откликаться (тиры отбора)** — там же, три ключа. Автоклик идёт по приоритету:
`tier1` — `core` + удалёнка, `tier2` — `core_wide` + удалёнка, `tier3` — любой из двух стеков,
но **офис** и только города из `office_cities`. Тир3 это не готовность к переезду, а ставка на
переговоры: в крупном городе офисная по описанию вакансия часто допускает удалённый формат.

```jsonc
"core_wide":      ["Django", "Flask", "PostgreSQL"],  // широкий стек для tier2
"office_cities":  [],                                 // пусто = tier3 ВЫКЛЮЧЕН, только удалёнка
"extra_exp_ids":  ["between3And6"]                    // опыт сверх exp_ids ТОЛЬКО для откликов
```

`extra_exp_ids` нужен потому, что вилку «3–6 лет» массово ставят на мидл-позиции: откликаться
уместно, а задирать сам `exp_ids` нельзя — поедет процент совпадения в ленте.

**Пустой список = настройка снята** (как пустая строка в `blacklists`), ключ отсутствует =
дефолт из `config.py`. Исключение — `search_queries` и `cities` ниже: там пусто уходит
в дефолт, потому что «искать нечего» это не предпочтение, а сломанный конфиг.

**Что и где собирать на hh.ru** — `search_queries` (список запросов) и `cities` (ключ —
`area id` из [api.hh.ru/areas](https://api.hh.ru/areas), значение — подпись в отчётах).
Профиль **заменяет** список целиком, а не дополняет: половинчатое слияние дало бы набор,
который человек не писал. Дефолт — 35 запросов и 34 города СНГ. Помните про лимит hh
в 2000 вакансий на пару «запрос+город»: узкие запросы вытаскивают то, что не влезло
в топ широкого.

```jsonc
"search_queries": ["python разработчик", "backend разработчик"],
"cities":         { "1": "Москва", "2": "Санкт-Петербург" }
```

**Письма** — два независимых набора:

- `cover_template` — письмо для **крон-откликов** (`cover.py`), одна строка. Подстановки
  `{name}` (тайтл) и `{employer}` (компания)
- `feed_cover_templates` — письма для **ленты** (кнопка «Другой вариант» в карточке), список.
  Подстановки `{role}` (тайтл в кавычках), `{company}` (` в компании X`), `{stack}` (фраза про
  совпавшие технологии). Список заменяет дефолты из `src/feed/cover.js`; счётчик «Вариант N
  из M» считает ваши шаблоны, а не дефолтные

Пусто в любом из них — дефолт из кода. Правя `src/feed/cover.js` вручную, вы правите
дефолты для всех; свой набор кладите в профиль.

**Ответы на анкеты-опросники** — `form_answers` **на верхнем уровне** профиля (не внутри
`answers`), список `{q, a, own, _note}`, где `q` это РЕГЕКС по тексту поля, а `own` — текст
для варианта «Свой вариант». Доступна подстановка `{age}` — возраст считается от
`answers.birth_date` на день заполнения, цифрой писать нельзя: протухнет в день рождения.
Пустой список значит, что движок отвечает только на вопрос о зарплате (детерминированный код
по грейду) и через LLM, а остальные поля уходят человеку. Наполнять по мере встречи новых
вопросов — точную формулировку показывает `python hh.py forms --dry`, он ничего не отправляет.

Рядом `answers.practices` — ответы про **процессы** («проводили регрессионное тестирование?»,
«настраивали мониторинг?»). Технологии в таком вопросе нет, по стеку он не разрешается, и без
записи движок молчит.

**Веса бейджа «% совпадения»** — `src/feed/resume.js`: `RESUME_WEIGHTS`, `EXP_SCORE`
(стек 60 + опыт 25 + удалёнка 15), `RESUME_REQUIRE_REMOTE`.

**Кнопка «по резюме»** — там же, `resume.js::matchesResume`: жёсткий фильтр из ДВУХ условий,
стек + удалёнка. Грейд в нём не участвует (см. [`docs/feed.md`](docs/feed.md)) — на скоринг
он по-прежнему влияет.

**Какие технологии упоминать в письме ленты** — `src/feed/cover.js`: `RESUME_TECH_SET`
и `TECH_LABEL` (сами тексты писем настраиваются профилем, см. выше).

Известный компромисс по тирам: лента знает только `core` и покажет «не матч» вакансиям
тиров 2–3, на которые крон реально откликнется.

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
