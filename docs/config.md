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

**`LOG_BODIES` и `config.py::body`.** Лог — не секретный, но ПЕРСОНАЛЬНЫЙ сток: на уровне
INFO туда попадали вопрос рекрутера, отправляемый ответ с фактами профиля и пары «поле
анкеты -> подставленное значение» (аудит 08.08.2026). Уровнем синка это не лечится —
строки пишутся `log.info`, — поэтому текст маскируется в точке вызова: `body(text)` отдаёт
только форму («`<412 симв.>`»), `body(text, keep=N)` оставляет первые N символов, когда
без них строку не опознать. `LOG_BODIES=1` возвращает дословную запись на время отладки.
Что маскируется, а что намеренно нет — `security.md`.

## Источники

- `SOURCES` — **9 значений**: `hh,hirify,talanto,getmatch,arbeitnow,himalayas,web3,themuse,jobicy`.
  Порядок задаёт победителя при кросс-портальном дедупе (`domain/dedup.py`): hh первый, потому
  что с него работает автоотклик. Первые четыре — рынок РФ, остальные пять — глобальный
  (см. `docs/collect.md`). Неизвестное имя -> warning + пропуск, а не падение.
- `PORTAL_SITES` — домен портала для подписи в карточке ленты (`hh` -> `hh.ru`, `web3` ->
  `web3.career`, …), 9 записей, по одной на источник. Живёт в Python и инжектится в
  `feed-data.js` (`PORTAL_SITES_PY`) — **дублировать в JS нельзя**, так уже разъезжались
  константы. Источник без записи здесь получает своё имя как есть, а не чужую подпись.
- `HIRIFY_PARAMS` — querystring фильтра hirify: skills + специализации, без ограничений
  по грейду и формату (~18k вакансий).
- `HIRIFY_ENRICH_MAX = 600` — потолок per-vacancy запросов за прогон.
- `TALANTO_PARAMS` — querystring фильтра talanto (`limit=100&sort=newest`, без `period` —
  все активные ~40–50k).
- `TALANTO_ENRICH_MAX = 600` — потолок per-vacancy запросов talanto за прогон.
- `GETMATCH_PAGE_SIZE = 100`, `GETMATCH_PAGE_CONCURRENCY = 4`,
  `GETMATCH_ENRICH_CONCURRENCY = 4`, `GETMATCH_ENRICH_MAX = 900` — getmatch.ru. Портал мелкий
  (~740 активных вакансий), поэтому лимиты скромнее, а `enrich_max` с запасом перекрывает
  весь объём за один прогон: сбор обязательно двухфазный, грейд и полное описание есть
  только в карточке.
- `ARBEITNOW_MAX_PAGES = 120`, `ARBEITNOW_PAGE_CONCURRENCY = 6` — обход arbeitnow (реально
  ~41 страница по 100). Обход рвётся на первой ЧЕСТНО пустой странице: пустая перепроверяется
  трижды с паузами 2/4/8 с, потому что под троттлингом портал отдаёт HTTP 200 с пустым
  списком, неотличимый от конца выдачи. Упор в сам лимит — уже не «страховка сработала»,
  а WARNING «обход УПЁРСЯ В ЛИМИТ N страниц»: значит выдача не исчерпана и лимит надо поднять.
- `HIMALAYAS_MAX_PAGES = 5000`, `HIMALAYAS_PAGE_CONCURRENCY = 6`,
  `HIMALAYAS_BATCH_PAUSE = 0.4` — обход himalayas. Страница жёстко 20, выдача кончается на
  ~4870-й, так что дефолт покрывает портал ЦЕЛИКОМ (обход рвётся на первой честно пустой
  странице, лишние итерации не тратятся). Замер 07.08.2026: 26 027 вакансий, 4813 страниц,
  41.9 мин. Прежние 2000 стояли из-за памяти — источник копил сырые карточки до конца обхода
  (731 МБ); теперь он нормализует и отсеивает не-IT пачками, и в памяти остаются только
  выжившие ~37 %. Конкурентность **снижена с 12 до 6** плюс пауза между пачками: портал
  уходил в троттлинг примерно на 1900-м запросе подряд и начинал отдавать пустые страницы,
  неотличимые от конца выдачи, — обход обрывался на 40 % и отчитывался как успешный.
- `THEMUSE_MAX_PAGES = 99`, `THEMUSE_PAGE_CONCURRENCY = 6`, `THEMUSE_MAX_AGE_DAYS = 60`,
  `THEMUSE_QUERIES` (9 комбинаций) — themuse.com. 99 — **жёсткий потолок портала**, а не наш
  выбор: страница 100 отдаёт HTTP 400, и в коде он продублирован `themuse.py::PAGE_CEILING`
  (берётся `min` из двух, поднять env-ручкой выше нельзя). Поэтому охват набирается
  комбинациями `category × level`, каждая до 1980 записей. `THEMUSE_MAX_AGE_DAYS` — граница
  гост-вакансии в домене (`freshness.GHOST_DAYS`): сортировки по дате у API нет, свежесть
  режется на нашей стороне.
- `JOBICY_COUNT = 100`, `JOBICY_REQUEST_CONCURRENCY = 4`, `JOBICY_INDUSTRIES` (11 значений) —
  jobicy.com. Пагинации у API нет вовсе, есть только `count` с потолком 100, поэтому охват
  набирается перебором индустрий. **Пустая строка в списке — не опечатка**: это запрос без
  фильтра, общая свежая выдача, которая даёт то, что не попало ни в одну индустрию.
- `GLOBAL_SOURCES_IT_ONLY = 1` — отсев не-IT на входе **пяти** глобальных источников:
  arbeitnow, himalayas, web3, themuse, jobicy (это общие job-борды: 66 % и 63 % не-IT).
  `0` — забирать всё, включая ритейл и логистику.
- **`WEB3_TOKEN`** — токен web3.career (бесплатный, по email на `web3.career/web3-jobs-api`).
  Пусто -> источник пропускается, сбор не падает. **Секрет: только в `.env`.**
- `WEB3_TAGS` — список тегов для перебора (у API нет пагинации, охват набирается тегами).
  Дефолт — **40 тегов**: стек, инфра/данные, грейдовые (junior/entry-level/intern), форматный
  `remote` и доменные (blockchain/defi/solidity/…). Замер: 1835 вакансий за 66 с.
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
  определения периода. Имена **исторические** (по первому потребителю, hirify), но пороги
  ОБЩИЕ: их читает `domain/salary.py::SalaryPeriod.infer`, единый для hirify и web3.career.
  Своих копий у адаптеров больше нет — до 07.08.2026 у web3 стоял свой порог 15 000, и один
  и тот же вопрос имел два разных ответа

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
- `WATCHDOG_DUMP_S = 40 мин` / `WATCHDOG_KILL_S = 50 мин` (`autoclick.py`, не env) — дамп
  стека и снос зависшего прогона. Дедлайн на ОТДЕЛЬНЫЙ Playwright-вызов невозможен: sync-API
  привязан к своему потоку, см. `docs/errors.md`. Пороги подтверждены замером 128 прогонов:
  здоровый занимает медиану 27 мин, p90 36, максимум 37.4 — снижение до 8 мин, которое
  когда-то предлагалось, срезало бы штатные прогоны

## Профиль резюме

`resume_profile.json` в корне — **единый источник** личных настроек: всё, что зависит от
конкретного соискателя, живёт здесь, а не в исходниках. Полный ключ-в-ключ пример с
пояснениями — `resume_profile.example.json`; страж соответствия примера и кода —
`tests/backend/test_profile_settings.py`.

Ключи и кто их читает:

- `core` -> `RESUME_CORE` — ядро стека, общее для autoclick-фильтра и скоринга ленты
  (инжектится в `feed-data.js` как `RESUME_CORE_PY`)
- `exp_ids` -> `RESUME_EXP_IDS` — допустимый опыт. Потребитель ОДИН —
  `candidates.py::APPLY_EXPS` (отбор под отклик); в ленту опыт больше не инжектится, мост
  `RESUME_EXPS_PY` удалён 08.08.2026 как мёртвый
- `blacklists` -> `APPLY_BLACKLISTS` — какие тайтлы не брать; значение — **список слов**
  (хвост `*` = совпадение по началу) или строка-регекс для продвинутых; пустой список
  выключает правило (`candidates.py::_rx`, `_words_to_rx`)
- `core_wide`, `office_cities`, `extra_exp_ids` — тиры отбора 2–3 (раздел «Фильтр отклика»)
- `search_queries`, `cities` — что и где собирать на hh (раздел «Классификаторы»)
- `cover_template` — письмо крон-откликов (`cover.py`); `feed_cover_templates` — письма
  ленты (инжектятся как `FEED_COVER_TEMPLATES_PY` -> `cover.js::coverTemplates`)
- `answers` — факты для шаблонных ответов бот-рекрутерам (`chat.md`), включая
  `practices` (ответы про процессы), `birth_date` (только для `{age}` в анкетах) и
  `tech_synonyms` — как ещё называют технологии стека (`{"<канон>": ["<написание>", …]}`,
  читает `chat_answer.py::_spelling_groups`). Профильный словарь ДОПОЛНЯЕТ дефолтный
  `chat_answer.py::_TECH_SYNONYMS`, а не заменяет его; значения — слова, не регексы.
  Нужен, чтобы движок не отвечал «нет, не работал» про собственный стек, названный иначе
- `form_answers` (верхний уровень) — словарь ответов на поля анкет (`form_fill.py`)

Раньше ядро дублировалось между `autoclick.APPLY_CORE` и `resume.js` и молча разошлось —
поэтому одно место и инжекция, а не копии.

**Коды опыта проверяются** (`config.py::_known_exp_ids`): `exp_ids` и `extra_exp_ids`
сверяются с ключами `EXP_LABELS`, неизвестный код даёт WARNING и выбывает из отбора.
Проверка появилась 08.08.2026 вместе с удалением моста `RESUME_EXPS_PY`: раньше опечатку
выдавал `KeyError` при сборке ленты, а без моста она перестала проявляться где-либо —
код просто ни с чем не совпадал и молча сужал отбор, то есть давал недоотклики без единого
признака в логе. **ПРЕДУПРЕЖДЕНИЕ, А НЕ ПАДЕНИЕ — осознанный компромисс:** правило «fail
fast на своих ошибках» относится к НАШИМ константам, а профиль пишет человек и читается
по контракту `_profile_list` (чужой тип -> предупреждение и дефолт). Плюс `config`
импортируется каждой командой: исключение здесь уронило бы и сбор, и отчёты из-за опечатки
в настройке отбора.

## Фильтр отклика

ВСЕ ТРИ ТИРА переопределяются профилем (08.08.2026); значения ниже — дефолты, действующие,
пока ключа в `resume_profile.json` нет.

- `APPLY_CORE_WIDE` <- `core_wide` — `{Django, Flask, PostgreSQL, MySQL, Redis, Kafka}` (тир 2)
- `APPLY_OFFICE_CITIES` <- `office_cities` — `{Москва, Санкт-Петербург, Тольятти, Самара}` (тир 3)
- `APPLY_EXTRA_EXP_IDS` <- `extra_exp_ids` — `["between3And6"]`, опыт СВЕРХ `exp_ids` только
  для отбора откликов: вилку «3–6 лет» массово ставят на мидл-позиции, куда откликаться
  уместно, а `exp_ids` пусть описывает реальный опыт

**Что `exp_ids` НЕ делает** (правка 08.08.2026): прежняя формулировка «менять нельзя —
поедет процент совпадения в ленте» неверна. Лента опыт из профиля не читала никогда
(`RESUME_EXPS_PY` в JS не потреблялся и удалён), а скоринг `resume.js::EXP_SCORE` ключуется
доменными КОДАМИ опыта и от профиля не зависит вовсе. Единственный потребитель обоих
ключей — `candidates.py::APPLY_EXPS`, то есть отбор под отклик.

Раньше тиры 2–3 жили только в коде, и в `APPLY_OFFICE_CITIES` буквально стоял город владельца
репозитория — чужой форк обязан был править исходник.

**Дефолтный senior-блеклист знает `Sr.` и «принципал»** (08.08.2026). Набор слов
`candidates.py::APPLY_SENIOR_BLACKLIST` синхронизирован со словарём грейдов
`domain/grade.py::_TITLE_RX`: до этого «Sr. Python Developer» проходил отбор как рядовая
вакансия и одновременно считался senior при ответе про деньги — один тайтл, две трактовки
в одном прогоне. Паритет с `resume_profile.example.json::blacklists.senior` держит страж
`tests/backend/test_profile_settings.py`.

**Пустой список в профиле ЗНАЧИМ** и в дефолт не проваливается (`config.py::_profile_list`):
`"office_cities": []` выключает тир 3 целиком — тот же контракт, что у пустой строки
в `blacklists`. Пустой набор безопасен: `techs & set()` и `city in set()` всегда ложны.
Чужой тип (строка вместо списка) -> предупреждение в лог и дефолт.

**Задокументированный компромисс**: лента знает только тир 1 (`core`) и покажет «не матч»
вакансиям тиров 2–3, на которые крон реально откликнется.

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

## LLM-заполнение форм-анкет (`form_fill`, OpenRouter)

LLM черновит ответ на поле анкеты **только из фактов резюме**; тул заполняет поля, но
**submit жмёт человек** в видимом окне — RFC-003. Транспорт тот же `OPENROUTER_API_KEY` /
`OPENROUTER_BASE_URL`, что у intent и rephrase.

- `FORM_MODEL` (env; дефолт = `INTENT_MODEL`)
- `FORM_TIMEOUT = 8` — сек
- `FORM_MAX_TOKENS = 300` — генерация
- `FORM_MAX_ANSWER_LEN = 1500` — жёсткий лимит длины ответа одного поля
- `FORMS_ENABLED` (env `FORMS_LLM`) — **по умолчанию OFF**, truthy-set ЯВНЫЙ
  (`1/true/yes/on`); `off`/`n` НЕ включают

Исходное «не в кроне» из RFC-003 **отменено осознанно**: `cron/cron_apply.bat` выставляет
`FORMS_LLM=1`, потому что без флага очередь анкет росла быстрее, чем вычерпывалась. Инвариант
не тронут — `try_autofill` шлёт только при полноте заполнения. Детали — `apply.md`,
`security.md`, `docs/rfc-003-form-fill.md`.

## Официальный API hh.ru (OAuth)

Второй путь рядом с браузерным. Проверено 28.07: `api.hh.ru` **не** закрыт DDoS-Guard —
запрос доходит до HH (`Server-Timing: frontik`), а без токена приходит
`{"errors":[{"value":"bad_authorization","type":"oauth"}]}`. То есть нужен OAuth-токен,
а не обход защиты. Ключи — своего приложения с `dev.hh.ru`.

- `HH_CLIENT_ID` / `HH_CLIENT_SECRET` — **секреты, только в `.env`**; пусто -> путь не работает
- `HH_REDIRECT_URI = http://localhost:8765/callback` — куда HH возвращает код авторизации
- `HH_API_UA = hr-work-applicant/1.0` — контакт в `HH-User-Agent`; **обязателен по правилам
  API**: по нему HH связывается при проблемах
- `HH_TOKEN_FILE = data/hh_token.json` — access/refresh, gitignored вместе с `data/`
- `HH_ACCESS_TOKEN` — ручной override токена (секрет)

## Классификаторы

- `SEARCH_QUERIES` <- `search_queries` — ~35 запросов: зонтичные + по языкам + по слоям +
  данные/ML + инфра/QA + мобильные + ниши. Голый `developer` **убран**: на HH он матчит
  девелопмент недвижимости и Business Development целиком, ~60 % не-IT.
- `CITIES` <- `cities` — 34 города (Россия + Беларусь); ключ = `area id` из `api.hh.ru/areas`,
  значение = подпись в отчётах

  Оба переопределяются профилем и заменяются ЦЕЛИКОМ, а не дополняются: половинчатое слияние
  дало бы набор, который человек не писал и не может предсказать. В отличие от тиров, пустое
  значение здесь уходит в дефолт — «искать нечего» это не предпочтение, а сломанный конфиг,
  который обнулил бы сбор. Правка любого из них инвалидирует кеш (`storage.md`).
- `TECH_PATTERNS` — ~55 regex. Отдельные хаки задокументированы на месте: `Go` требует
  dev-контекста рядом (иначе ловит «Яндекс Go»); `C#` без хвостового `\b` (после `#`
  границы слова нет).
- `LANG_KEYS` — только языки, для зарплатного анализа
- `ROLE_PATTERNS` — **16 ключей**, **порядок важен** (первое совпадение). Ключи обязаны
  совпадать со значениями `Role`, иначе `ValueError` на импорте `parsing.py`. В самом `Role`
  членов 17: `NON_IT` — доменный дефолт, регекса под него в карте нет (не-IT отсекает
  `parsing._HARD_NON_IT` и фолбэк). Роли `Support` в карте нет намеренно — поддержка
  отсекается раньше.
- `EXP_LABELS` — подписи уровней опыта

## Палитра дашборда

`BG` · `PAPER` · `GRID` · `TEXT` · `ACCENT`.

Цвета свежести здесь **не живут** — они переехали в `FreshnessClass.color`, потому что
раньше подписи были в одном файле, цвета в другом, и словари молча разошлись.

## Переменные окружения

Из `.env` или окружения. Список ПОЛНЫЙ — ровно то, что читают `os.getenv` в `config.py`,
`apply/autoclick.py`, `apply/cover.py`, `net/proxy.py` и `infrastructure/search.py`; шаблон
`.env.example` обещает читателю именно этот раздел, поэтому расхождение в любую сторону —
дефект. Сверка: `grep -rn "os.getenv" hrwork/ hh.py`.

**Секреты:** `HH_EMAIL`, `HH_PASSWORD` (автовход), `HH_ACCESS_TOKEN`, `HH_CLIENT_ID`,
`HH_CLIENT_SECRET`, `WEB3_TOKEN`, `OPEN_ROUTER_API_KEY`, `ANTHROPIC_API_KEY`
(письма `--cover llm`, читает `cover.py::llm_cover`; пусто -> шаблон),
`PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD`.

**Источники:** `SOURCES`, `HIRIFY_PARAMS`, `HIRIFY_ENRICH_MAX`, `HIRIFY_PAGE_CONCURRENCY`,
`HIRIFY_ENRICH_CONCURRENCY`, `TALANTO_PARAMS`, `TALANTO_ENRICH_MAX`,
`TALANTO_PAGE_CONCURRENCY`, `TALANTO_ENRICH_CONCURRENCY`, `GETMATCH_PAGE_SIZE`,
`GETMATCH_PAGE_CONCURRENCY`, `GETMATCH_ENRICH_CONCURRENCY`, `GETMATCH_ENRICH_MAX`,
`ARBEITNOW_MAX_PAGES`, `ARBEITNOW_PAGE_CONCURRENCY`, `HIMALAYAS_MAX_PAGES`,
`HIMALAYAS_PAGE_CONCURRENCY`, `HIMALAYAS_BATCH_PAUSE`, `WEB3_TAGS`, `WEB3_TAG_CONCURRENCY`,
`THEMUSE_MAX_PAGES`, `THEMUSE_PAGE_CONCURRENCY`, `THEMUSE_MAX_AGE_DAYS`, `THEMUSE_QUERIES`,
`JOBICY_COUNT`, `JOBICY_INDUSTRIES`, `JOBICY_REQUEST_CONCURRENCY`, `GLOBAL_SOURCES_IT_ONLY`.

**Поведение:** `DESC_CACHE_MAX_AGE_DAYS`, `COLLECT_MIN_RATIO`, `COLLECT_SANITY_MIN`,
`CURL_MAX_TIME`, `HTTP_BACKEND`, `HH_ENRICH_BATCH_MULT`, `HH_CONCURRENCY`, `CACHE_TTL_HOURS`,
`SERVE_PORT`, `HH_DAILY_APPLY_CAP`, `APPLY_SKIP_STREAK_MAX`, `HH_DISABLE_PROXIES`,
`HH_REDIRECT_URI`, `HH_API_UA`, `LOG_BODIES` (дословные тела в логе, см. «Пути»).

**Поиск (`infrastructure/search.py`, не `config.py`):** `SEARCH_TABLE`,
`SEARCH_WORK_MEM` (`work_mem` на соединении и на пуле, дефолт `64MB` — на нём сняты все
замеры `/api/search`), `SEARCH_POOL_MAX` (размер пула, дефолт 8).

**LLM (все три пути — opt-in, по умолчанию выключены):** `OPENROUTER_BASE_URL`,
`INTENT_MODEL`, `INTENT_TIMEOUT`, `INTENT_LLM`, `REPHRASE_MODEL`, `REPHRASE_TIMEOUT`,
`REPHRASE_MAX_TOKENS`, `REPHRASE_LLM`, `FORM_MODEL`, `FORM_TIMEOUT`, `FORM_MAX_TOKENS`,
`FORM_MAX_ANSWER_LEN`, `FORMS_LLM`.

`.env` в репозиторий не коммитится.

## Проверка

```
python -c "from hrwork.config import CACHE_TTL_HOURS, SOURCES; print(CACHE_TTL_HOURS, SOURCES)"
CACHE_TTL_HOURS=20 python -c "from hrwork.config import CACHE_TTL_HOURS; print(CACHE_TTL_HOURS)"
```

Ожидаемо: `24 ['hh', 'hirify', 'talanto', 'getmatch', 'arbeitnow', 'himalayas', 'web3',
'themuse', 'jobicy']`, затем `20`.
