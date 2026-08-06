# Конфигурация

**Файл:** `hrwork/config.py` — единый модуль констант. `.env` в корне проекта
(`load_dotenv`), **реальные переменные окружения имеют приоритет**.

`config.py` сознательно оставлен god-модулем: разнос по слоям дал бы десяток файлов
по 20 строк и циклические импорты. Пересмотреть, если появится второй потребитель пакета.

## Пути

`BASE_DIR` (корень) · `DATA_DIR` · `LOGS_DIR` · `RAW_FILE` (`vacancies_raw.json`) ·
`META_FILE` (`cache_meta.json`) · `REPORTS_DIR` · `TEMPLATE_DIR` · `DASHBOARD_OUT` · `FEED_OUT`.

`DATA_DIR` и `LOGS_DIR` создаются на импорте.

**`LOGS_DIR` (`logs/`) — единственный источник пути к логам.** Отделён от `DATA_DIR`
по назначению: `data/` читается кодом и бэкапится, `logs/` только пишется и ротируется.
Крон-обёртки в `cron/` перенаправляют вывод туда же (`>> logs\cron_*.log`), так что новых
лог-файлов вне `logs/` появляться не должно.

## Источники

- `SOURCES` — `hh,hirify,talanto,getmatch,arbeitnow,himalayas`. Порядок задаёт победителя при
  кросс-портальном дедупе (`domain/dedup.py`): hh первый, потому что с него работает автоотклик.
  Первые четыре — рынок РФ, последние два — глобальный (см. `docs/collect.md`).
- `HIRIFY_PARAMS` — querystring фильтра hirify: skills + специализации, без ограничений
  по грейду и формату (~18k вакансий).
- `HIRIFY_ENRICH_MAX = 600` — потолок per-vacancy запросов за прогон.
- `TALANTO_PARAMS` — querystring фильтра talanto (`limit=100&sort=newest`, без `period` —
  все активные ~40–50k).
- `TALANTO_ENRICH_MAX = 600` — потолок per-vacancy запросов talanto за прогон.
- `ARBEITNOW_MAX_PAGES = 120`, `ARBEITNOW_PAGE_CONCURRENCY = 6` — обход arbeitnow (реально
  ~41 страница по 100; лимит только страховка, обход рвётся на первой пустой).
- `HIMALAYAS_MAX_PAGES = 2000`, `HIMALAYAS_PAGE_CONCURRENCY = 12` — обход himalayas. Страница
  жёстко 20, выдача кончается на ~4870-й; дефолт берёт свежие ~40k карточек за 8.7 мин.
  **Поднимать до полных 4900 без переделки сбора нельзя:** источник копит все записи в памяти
  до возврата, и на дефолтных 2000 страницах процесс занимал 731 МБ (замер 07.08.2026) —
  на 4900 это ~1.8 ГБ, а прогон идёт вместе с hh и talanto. Полный охват сперва требует
  постраничной отдачи вместо накопления списка.
- `GLOBAL_SOURCES_IT_ONLY = 1` — отсев не-IT на входе arbeitnow/himalayas/web3 (это общие
  job-борды: 66 % и 63 % не-IT). `0` — забирать всё, включая ритейл и логистику.
- **`WEB3_TOKEN`** — токен web3.career (бесплатный, по email на `web3.career/web3-jobs-api`).
  Пусто -> источник пропускается, сбор не падает. **Секрет: только в `.env`.**
- `WEB3_TAGS` — список тегов для перебора (у API нет пагинации, охват набирается тегами).
  Дефолт — 40 тегов под профиль плюс грейдовые/форматные. Замер: 1835 вакансий за 66 с.
- `WEB3_TAG_CONCURRENCY = 6` — тегов параллельно.
- `DESC_CACHE_MAX_AGE_DAYS = 14` — предохранитель от тихой правки описания без смены
  сигнала. `0` = чистый signal-based режим.

## Санити-гейт сбора

- `COLLECT_MIN_RATIO = 0.5` — порог просадки, ниже которого источник считается сбойным
- `COLLECT_SANITY_MIN = 500` — минимальный прошлый объём, при котором гейт вообще включается

Подробнее — `collect.md`.

## Сеть и конкурентность

- `CURL_MAX_TIME = 25` — таймаут одного HTTP-запроса
- `HTTP_BACKEND = curl` — либо `httpx` (пул keep-alive, экспериментальный)
- `CONCURRENCY = 5` (`HH_CONCURRENCY`) · `PAGE_DELAY = 0.25` · `HH_ENRICH_BATCH_MULT = 4`
- `HIRIFY_PAGE_CONCURRENCY = 10` · `HIRIFY_ENRICH_CONCURRENCY = 8`
- `TALANTO_PAGE_CONCURRENCY = 8` · `TALANTO_ENRICH_CONCURRENCY = 8`
- `PER_PAGE = 100` · `MAX_PAGES = 20` -> потолок 2000 вакансий на город
- `CACHE_TTL_HOURS = 24` — крон ставит **20**, чтобы суточный запуск не попадал ровно
  на границу и поведение не плавало

## Нормализация зарплат

- `NET_FROM_GROSS = 0.87` — вычет НДФЛ 13 %
- `WORK_HOURS_PER_MONTH = 160` · `MONTHS_PER_YEAR = 12`
- `HIRIFY_HOURLY_MAX_USD = 300` · `HIRIFY_YEARLY_MIN_USD = 25 000` — границы эвристики
  определения периода

## Пороги выборки

- `MIN_SAMPLE_SALARY = 5` — минимум вакансий для показа медианы по языку/опыту
- `MIN_SAMPLE_CITY_LANG = 3` — для ячейки город × язык (срез мельче, порог ниже)

Ниже порога срез не показывается вовсе — медиана по двум вакансиям вводит в заблуждение.

## Сервер и отклики

- `SERVE_PORT = 8000`
- `HH_DAILY_APPLY_CAP = 200` — суточный лимит HH
- `APPLY_SKIP_STREAK_MAX = 50` — сколько вакансий ПОДРЯД без кнопки отклика считать
  блокировкой HH, а не архивом; на пороге прогон останавливается. Замер 19 суток лога:
  в здоровые сутки максимальная серия 8 и 21, при блокировке — 97, 150, 268
- `WATCHDOG_KILL_S = 8 мин` (`autoclick.py`, не env) — снос зависшего прогона. Дедлайн на
  ОТДЕЛЬНЫЙ Playwright-вызов невозможен: sync-API привязан к своему потоку, см. docs/errors.md

## Профиль резюме

`resume_profile.json` в корне — **единый источник** для autoclick-фильтра и скоринга ленты.

- `RESUME_CORE` — технологии-ядро (нужна хотя бы одна)
- `RESUME_EXP_IDS` — допустимый опыт

Раньше ядро дублировалось между `autoclick.APPLY_CORE` и `resume.js` и молча разошлось.
Теперь правится в одном файле, `feed.py` инжектит его в `feed-data.js`.

Блок `answers` того же файла питает шаблонные ответы (`chat.md`).

## Фильтр отклика

- `APPLY_CORE_WIDE` — `{Django, Flask, PostgreSQL, MySQL, Redis, Kafka}` (тир 2)
- `APPLY_OFFICE_CITIES` — `{Москва, Санкт-Петербург, Тольятти, Самара}` (тир 3)

**Задокументированный компромисс** (`config.py::APPLY_CORE_WIDE`): тир 1 общий с лентой через
`resume_profile.json`, а тиры 2–3 живут только здесь. Лента про них не знает и покажет
«не матч» вакансиям, на которые крон реально откликнется. Захотим синхронности — переносить
в `resume_profile.json`.

## LLM-классификатор намерения (OpenRouter)

`chat_intent` маршрутизирует вопросы бот-рекрутеров через LLM (различает `years` /
`years_tech` / `depth`, где regex хрупок). LLM возвращает только метку, факты — локально.

- `OPENROUTER_API_KEY` (env `OPEN_ROUTER_API_KEY`) — ключ OpenRouter; пусто -> классификатор
  отключён, откат на regex
- `OPENROUTER_BASE_URL = https://openrouter.ai/api/v1` — OpenAI-совместимый шлюз
- `INTENT_MODEL = deepseek/deepseek-v4-flash` — модель; смена = строка (провайдеро-нейтрально)
- `INTENT_TIMEOUT = 6` — сек; таймаут -> откат на regex
- `INTENT_ENABLED` (env `INTENT_LLM`) — **по умолчанию OFF**. Без `INTENT_LLM=1` сеть не
  дёргается даже при наличии ключа; движок работает на regex. Opt-in как `cover mode='llm'`.

Промпт классификатора **не содержит фактов профиля** — наружу уходит только текст вопроса
(приватность + «нечем соврать»). Детали — `security.md`, `chat.md`.

## LLM-переформулировка ответа (`chat_rephrase`, OpenRouter)

`chat_rephrase` переформулирует УЖЕ ОДОБРЕННЫЙ ответ под вопрос (etap 2, human-gated).
Валидатор `_grounded` режет ново-токенные фабрикации, человек подтверждает каждую изменённую
формулировку `[y/N]`; без tty/крон — уходит источник дословно. Транспорт — тот же
`OPENROUTER_API_KEY`/`OPENROUTER_BASE_URL`, что у intent.

- `REPHRASE_MODEL` (env; дефолт = `INTENT_MODEL`, тот же DeepSeek V4 Flash)
- `REPHRASE_TIMEOUT = 8` — сек; таймаут -> источник дословно
- `REPHRASE_MAX_TOKENS = 300` — генерация (не 30-токенная метка intent)
- `REPHRASE_ENABLED` (env `REPHRASE_LLM`) — **по умолчанию OFF**, truthy-set ЯВНЫЙ
  (`1/true/yes/on`); любое иное (`off`, `n`) НЕ включает. Двойной гейт: env И флаг `--rephrase`.

Наружу уходит **один одобренный факт + вопрос** — не профиль/резюме/переписка. Детали —
`security.md`, `chat.md`, RFC-002.

## Классификаторы

- `SEARCH_QUERIES` — ~35 запросов: зонтичные + по языкам + по слоям + данные/ML + инфра/QA +
  мобильные + ниши. Голый `developer` **убран**: на HH он матчит девелопмент недвижимости и
  Business Development целиком, ~60 % не-IT.
- `CITIES` — 34 города (Россия + Беларусь)
- `TECH_PATTERNS` — ~55 regex. Отдельные хаки задокументированы на месте: `Go` требует
  dev-контекста рядом (иначе ловит «Яндекс Go»); `C#` без хвостового `\b` (после `#`
  границы слова нет).
- `LANG_KEYS` — только языки, для зарплатного анализа
- `ROLE_PATTERNS` — 17 ролей, **порядок важен** (первое совпадение). Ключи обязаны совпадать
  со значениями `Role`, иначе `ValueError` на импорте `parsing.py`.
- `EXP_LABELS` — подписи уровней опыта

## Палитра дашборда

`BG` · `PAPER` · `GRID` · `TEXT` · `ACCENT`.

Цвета свежести здесь **не живут** — они переехали в `FreshnessClass.color`, потому что
раньше подписи были в одном файле, цвета в другом, и словари молча разошлись.

## Переменные окружения

Из `.env` или окружения:

**Секреты:** `HH_EMAIL`, `HH_PASSWORD` (автовход), `HH_ACCESS_TOKEN`,
`PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD`.

**Поведение:** `SOURCES`, `HIRIFY_PARAMS`, `HIRIFY_ENRICH_MAX`, `TALANTO_PARAMS`,
`ARBEITNOW_MAX_PAGES`, `ARBEITNOW_PAGE_CONCURRENCY`, `HIMALAYAS_MAX_PAGES`,
`HIMALAYAS_PAGE_CONCURRENCY`, `GLOBAL_SOURCES_IT_ONLY`,
`TALANTO_ENRICH_MAX`, `TALANTO_PAGE_CONCURRENCY`, `TALANTO_ENRICH_CONCURRENCY`,
`DESC_CACHE_MAX_AGE_DAYS`, `COLLECT_MIN_RATIO`, `COLLECT_SANITY_MIN`, `CURL_MAX_TIME`,
`HTTP_BACKEND`, `HIRIFY_PAGE_CONCURRENCY`, `HIRIFY_ENRICH_CONCURRENCY`, `HH_ENRICH_BATCH_MULT`,
`HH_CONCURRENCY`, `CACHE_TTL_HOURS`, `SERVE_PORT`, `HH_DAILY_APPLY_CAP`,
`HH_DISABLE_PROXIES`, `SEARCH_TABLE`.

`.env` в репозиторий не коммитится.

## Проверка

```
python -c "from hrwork.config import CACHE_TTL_HOURS, SOURCES; print(CACHE_TTL_HOURS, SOURCES)"
CACHE_TTL_HOURS=20 python -c "from hrwork.config import CACHE_TTL_HOURS; print(CACHE_TTL_HOURS)"
```

Ожидаемо: `24 ['hh', 'hirify', 'talanto']`, затем `20`.
