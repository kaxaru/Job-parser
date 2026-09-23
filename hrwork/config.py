"""Все настройки и константы пакета hrwork."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from hrwork.accounts import resolve_account
from hrwork.domain.account import AccountError

# Логгер пакета: весь hrwork берёт его как `from hrwork.config import log`. Присваивание, а не
# `import logger as log`, — чтобы это был настоящий атрибут модуля, а не неявный реэкспорт
# (mypy --strict, no_implicit_reexport: иначе 24 ошибки во всех импортирующих модулях).
log = logger

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).resolve().parent.parent   # hr_work/

# Аккаунт читается ДО .env (RFC-004): строка HR_ACCOUNT в .env сделала бы вторым аккаунтом
# КАЖДЫЙ процесс — сервер ленты, сбор, крон основного. Аккаунт — свойство запуска, не машины.
_HR_ACCOUNT_RAW = os.environ.get('HR_ACCOUNT', '')

# .env в корне проекта; реальные переменные окружения имеют приоритет
load_dotenv(BASE_DIR / '.env')

# ─── Search DB (PostgreSQL для /api/search; ядро-скрапер БД не использует) ───────
# Читается ПОСЛЕ load_dotenv -> берёт значения из .env (если заданы), иначе дефолты.
# Единственный источник DSN: search.py и спайк (load.py/bench.py) импортируют отсюда.
PG_DSN = {
    'host':     os.getenv('PGHOST', 'localhost'),
    'port':     int(os.getenv('PGPORT', '5433')),
    'dbname':   os.getenv('PGDATABASE', 'hh'),
    'user':     os.getenv('PGUSER', 'hh'),
    'password': os.getenv('PGPASSWORD', 'hh'),
}

DATA_DIR     = BASE_DIR / 'data'
# Логи — ОТДЕЛЬНО от данных: data/ это состояние и артефакты сборки, их бэкапят и читают
# кодом; логи же только пишутся и ротируются. Раньше лежали вперемешку, и data/ разрастался
# 25 лог-файлами, среди которых терялись marks.json и журнал откликов.
LOGS_DIR     = BASE_DIR / 'logs'
RAW_FILE     = DATA_DIR / 'vacancies_raw.json'
META_FILE    = DATA_DIR / 'cache_meta.json'
REPORTS_DIR  = DATA_DIR / 'reports'
TEMPLATE_DIR = BASE_DIR / 'templates'
DASHBOARD_OUT = DATA_DIR / 'dashboard.html'
FEED_OUT      = DATA_DIR / 'feed.html'

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Аккаунт HH (RFC-004) ─────────────────────────────────────────────────────
# Выбирается ТОЛЬКО переменной окружения до импорта: пути и профиль ниже пекутся один раз.
# ACCOUNT_DIR — состояние аккаунта (сессия, браузер, квота, журнал, статусы); для main это
# сам DATA_DIR, то есть прежние пути. Общее для всех (кеш вакансий, лента, marks, lock)
# остаётся на DATA_DIR. Ошибка разрешения — останов с подсказкой, а не трейсбек.
try:
    ACCOUNT = resolve_account(_HR_ACCOUNT_RAW, DATA_DIR)
except AccountError as _account_error:
    raise SystemExit(str(_account_error)) from None
ACCOUNT_DIR = ACCOUNT.data_dir

# ─── Logging ──────────────────────────────────────────────────────────────────

log.remove()
log.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
    level="DEBUG",
    colorize=True,
)
# Файл лога — СВОЙ НА ПРОЦЕСС (…_<pid>.log). Раньше все писали в один hr_work.log, и после
# развязки задач (синк перестал брать autoclick.lock и побежал параллельно с serve/apply)
# ротация начала падать: на Windows нельзя переименовать файл, который держит другой процесс
# -> PermissionError [WinError 32] обрывал прогон (20.07: синк умер на 550/600).
# Один писатель на файл — ротации нечего делить.
log.add(
    LOGS_DIR / f"hr_work_{os.getpid()}.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
    level="DEBUG",
    rotation="5 MB",
    retention=3,
    encoding="utf-8",
    delay=True,
    enqueue=True,          # запись через очередь: не блокирует прогон на I/O
)

# Дословные тела сообщений в логе — только по явному запросу на время отладки.
LOG_BODIES = os.getenv('LOG_BODIES', '').strip().lower() in ('1', 'true', 'yes', 'on')


def body(text: object, keep: int = 0) -> str:
    """Тело чужого/личного сообщения для лога: по умолчанию форма без содержания.

    `logs/*.log` и `logs/cron_*.log` — не секретный, но ПЕРСОНАЛЬНЫЙ сток: в них на уровне
    INFO попадали вопрос рекрутера, полный отправляемый ответ с фактами профиля и пары
    «поле анкеты -> подставленное значение» (включая зарплату). Секретов там нет —
    пароль прокси, куки и ключи проверены отдельно (аудит 08.08.2026), — но готовый дамп
    переписки на диске тоже лишний, а `retention=3` держит его неделями.

    Уровнем синка это не лечится: перечисленные строки пишутся `log.info`, а не `log.debug`.
    Поэтому маскируем в точке вызова. `LOG_BODIES=1` возвращает дословную запись.

    `keep` — сколько первых символов оставить видимыми, когда без них строку не опознать
    (например, чтобы отличить один вопрос анкеты от другого).
    """
    s = str(text)
    if LOG_BODIES:
        return s
    if keep > 0 and len(s) > keep:
        return f"{s[:keep]}… <{len(s)} симв.>"
    return f"<{len(s)} симв.>" if s else "<пусто>"

# ─── API / collection settings ────────────────────────────────────────────────

# Мульти-портальный агрегатор: какие источники собираем (реестр в data/sources.py).
# Порядок задаёт владельца id при дедупе (первое вхождение). hh — основной (с автооткликом),
# hirify — доп. портал (JSON-API, только просмотр/аналитика).
# greenhouse/ashby/devitjobs стоят В КОНЦЕ намеренно. Ключ кросс-портальной дедупликации —
# пара (работодатель, тайтл), и порядок решает, чья карточка победит. Поставить ATS первыми
# заманчиво (ссылка вела бы на борд самого работодателя, а не на агрегатор), но это
# переписало бы владельца у ~1900 уже собранных вакансий и сдвинуло бы все срезы по
# источникам разом. Прирост охвата от порядка НЕ зависит: новые записи всё равно наши,
# спорные — остаются за старыми источниками. Менять порядок — отдельным решением и с замером.
SOURCES = [s.strip() for s in os.getenv(
    'SOURCES',
    'hh,hirify,talanto,getmatch,arbeitnow,himalayas,web3,themuse,jobicy,'
    'greenhouse,ashby,devitjobs',
).split(',') if s.strip()]
# Домен портала для подписи в карточке ленты. Живёт в Python и инжектится в feed-data.js
# (PORTAL_SITES_PY) — дублировать в JS нельзя, так уже разъезжались константы. Источник
# без записи здесь получает своё имя как есть, а не чужую подпись.
PORTAL_SITES = {
    'hh':        'hh.ru',
    'hirify':    'hirify.me',
    'talanto':   'talanto.work',
    'getmatch':  'getmatch.ru',
    'arbeitnow': 'arbeitnow.com',
    'himalayas': 'himalayas.app',
    'web3':      'web3.career',
    'themuse':   'themuse.com',
    'jobicy':    'jobicy.com',
    # ATS отдают борд КАЖДОГО работодателя отдельно, общего сайта-витрины у источника нет.
    # Подпись — имя платформы: она честно отвечает на вопрос «откуда это у нас».
    'greenhouse': 'greenhouse.io',
    'ashby':      'ashbyhq.com',
    'devitjobs':  'devitjobs.uk',
}
# Фильтр hirify (querystring API). Широкий: интересующие skills+специализации, БЕЗ
# ограничений по грейду/формату/английскому/типу удалёнки (все значения). ~18k вакансий.
HIRIFY_PARAMS = os.getenv(
    'HIRIFY_PARAMS',
    'skills=python,startup,react,typescript,sql,ai,postgresql,api'
    '&specializations=backend_dev,fullstack_dev,frontend_dev,web_dev,'
    'qa_testing,data_analytics,business_analytics,data_science_ml',
)
# Полные описания (доп. запрос на вакансию) тянем только если всего ≤ порога — иначе tldr
# (на 18k per-vacancy fetch непрактичен и грузит hirify). Через env можно поднять.
HIRIFY_ENRICH_MAX = int(os.getenv('HIRIFY_ENRICH_MAX', '600'))
# Инкрементальный кеш описаний: запись переиспользуется, пока вакансия в выдаче и сигнал
# (_sig) не менялся. Предохранитель от тихой правки без смены сигнала — пере-обогащать
# записи старше N дней. 0 -> без ограничения по времени (чистый signal-based).
DESC_CACHE_MAX_AGE_DAYS = int(os.getenv('DESC_CACHE_MAX_AGE_DAYS', '14'))
# Санити-гейт от затирания полного кеша деградированным срезом (транзиентный блок источника
# на этапе 1). Если источник с прошлым объёмом >= COLLECT_SANITY_MIN просел ниже
# COLLECT_MIN_RATIO от прошлого — сбор считаем сбойным и НЕ перезаписываем кеш (обойти: --force).
COLLECT_MIN_RATIO = float(os.getenv('COLLECT_MIN_RATIO', '0.5'))
COLLECT_SANITY_MIN = int(os.getenv('COLLECT_SANITY_MIN', '500'))

# ─── Сеть / конкурентность сбора ──────────────────────────────────────────────
CURL_MAX_TIME = int(os.getenv('CURL_MAX_TIME', '25'))          # таймаут одного HTTP-запроса, сек
# Сетевой бэкенд сбора: 'curl' (по умолчанию — процесс на запрос, стабилен на Windows+VPN) или
# 'httpx' (пул keep-alive, быстрее без прокси). Экспериментальный — A/B на своей сети.
HTTP_BACKEND = os.getenv('HTTP_BACKEND', 'curl').strip().lower()
HIRIFY_PAGE_CONCURRENCY = int(os.getenv('HIRIFY_PAGE_CONCURRENCY', '10'))   # параллельных страниц списка
HIRIFY_ENRICH_CONCURRENCY = int(os.getenv('HIRIFY_ENRICH_CONCURRENCY', '8'))  # параллельных /slug
# talanto.work — публичный JSON-API (limit/offset). Без period — все активные (~40k);
# period=month срезал бы до ~22k, но свежесть и так фильтруют fresh/ghost-классы.
TALANTO_PARAMS = os.getenv('TALANTO_PARAMS', 'limit=100&sort=newest')
TALANTO_PAGE_CONCURRENCY = int(os.getenv('TALANTO_PAGE_CONCURRENCY', '8'))    # параллельных страниц
TALANTO_ENRICH_CONCURRENCY = int(os.getenv('TALANTO_ENRICH_CONCURRENCY', '8'))  # параллельных /jobs/{id}
TALANTO_ENRICH_MAX = int(os.getenv('TALANTO_ENRICH_MAX', '600'))              # описаний за прогон
# getmatch.ru — публичный JSON-API отобранных IT-вакансий (~740 активных). Мелкий портал,
# поэтому лимиты скромнее: enrich_max с запасом перекрывает весь объём за один прогон.
GETMATCH_PAGE_SIZE = int(os.getenv('GETMATCH_PAGE_SIZE', '100'))              # offset/limit выдачи
GETMATCH_PAGE_CONCURRENCY = int(os.getenv('GETMATCH_PAGE_CONCURRENCY', '4'))  # параллельных страниц
GETMATCH_ENRICH_CONCURRENCY = int(os.getenv('GETMATCH_ENRICH_CONCURRENCY', '4'))  # параллельных /offers/{id}
GETMATCH_ENRICH_MAX = int(os.getenv('GETMATCH_ENRICH_MAX', '900'))            # карточек за прогон
# arbeitnow.com — глобальный/европейский JSON-API. Однофазный: описание приходит в списке,
# поэтому enrich-лимитов нет. Замер 07.08.2026: 41 страница × 100 ≈ 4100 вакансий; max_pages
# с запасом, обход всё равно останавливается на первой пустой странице.
ARBEITNOW_MAX_PAGES = int(os.getenv('ARBEITNOW_MAX_PAGES', '120'))            # страховка от бесконечного обхода
ARBEITNOW_PAGE_CONCURRENCY = int(os.getenv('ARBEITNOW_PAGE_CONCURRENCY', '6'))  # страниц в пачке
# himalayas.app — global remote. Страница жёстко 20 (портал игнорирует limit>20).
# Замер 07.08.2026: выдача кончается на offset ~97 300, то есть ~4870 страниц и ~50 суток
# вглубь. Дефолт покрывает её ЦЕЛИКОМ (обход рвётся на первой пустой странице, так что
# лишние итерации не тратятся). Раньше здесь стояло 2000 из-за памяти — источник копил
# сырые карточки до конца обхода; теперь он нормализует и отсеивает не-IT пачками
# (см. himalayas.py::collect), и в памяти остаются только выжившие ~37 %.
HIMALAYAS_MAX_PAGES = int(os.getenv('HIMALAYAS_MAX_PAGES', '5000'))
# Конкурентность СНИЖЕНА с 12 до 6, плюс пауза между пачками: портал уходил в троттлинг
# примерно на 1900-м запросе подряд и начинал отдавать пустые страницы, неотличимые от
# конца выдачи, — обход обрывался на 40 % и отчитывался как успешный (07.08.2026).
HIMALAYAS_PAGE_CONCURRENCY = int(os.getenv('HIMALAYAS_PAGE_CONCURRENCY', '6'))
HIMALAYAS_BATCH_PAUSE = float(os.getenv('HIMALAYAS_BATCH_PAUSE', '0.4'))   # сек между пачками
# arbeitnow и himalayas — ОБЩИЕ job-борды, а не IT-порталы: замер 07.08.2026 дал 66 % и 63 %
# не-IT (ритейл, продажи, логистика, медицина), причём доля ровная по всей глубине выдачи.
# У профильных источников она 5–37 % (hirify 5, getmatch 9, talanto 28, hh 37), там фильтровать
# нечего. Здесь же 67k посторонних записей удвоили бы кеш и откатили оптимизацию его загрузки
# (67 -> 29 с), ничего не дав CRM по IT-вакансиям. Отсев по role.is_it — на входе источника.
GLOBAL_SOURCES_IT_ONLY = os.getenv('GLOBAL_SOURCES_IT_ONLY', '1') != '0'
# web3.career — web3/крипто-рынок. Токен бесплатный, но ОБЯЗАТЕЛЕН: без него API редиректит
# на форму регистрации. Пусто -> источник пропускается (не падает).
WEB3_TOKEN = os.getenv('WEB3_TOKEN', '').strip()
# Пагинации у API НЕТ (проверены page/offset/skip/start/p — все отдают ту же сотню), потолок
# limit=100. Поэтому охват набирается перебором ТЕГОВ: каждый отдаёт свои до ста.
# Список задан явно, а НЕ парсится с главной портала: разметка может измениться в любой
# момент, и молча опустевший список означал бы молча опустевший сбор. Отобраны теги под
# профиль (backend/data/инженерия) плюс грейдовые и форматные — они дают срез по всему
# порталу, а не только по стеку. Замер 07.08.2026 сделан на 17 тегах и дал 957 уникальных
# вакансий; список с тех пор расширен до 40 тегов, и цифра 957 к нему НЕ относится —
# перезамерить, если понадобится оценка объёма (аудит 08.08.2026 нашёл этот дрейф).
WEB3_TAGS = [t.strip() for t in os.getenv('WEB3_TAGS', ','.join((
    'python', 'backend', 'golang', 'rust', 'node', 'typescript', 'javascript', 'java',
    'sql', 'postgres', 'mongodb', 'redis', 'aws', 'docker', 'kubernetes', 'devops',
    'infrastructure', 'cloud-engineer', 'data-science', 'ai', 'machine-learning',
    'analyst', 'full-stack', 'front-end', 'engineer', 'dev', 'developer-relations',
    'junior', 'entry-level', 'intern', 'remote', 'blockchain', 'defi', 'crypto',
    'solidity', 'evm', 'layer-2', 'cryptography', 'security', 'gaming',
))).split(',') if t.strip()]
WEB3_TAG_CONCURRENCY = int(os.getenv('WEB3_TAG_CONCURRENCY', '6'))   # тегов параллельно
# jobicy.com — только удалёнка, ключ не нужен. Пагинации у API нет (есть лишь `count`),
# поэтому охват набирается перебором ИНДУСТРИЙ, как теги у web3. Пустая строка в списке —
# запрос без фильтра (общая свежая выдача), она даёт то, что не попало ни в одну индустрию.
JOBICY_COUNT = int(os.getenv('JOBICY_COUNT', '100'))   # потолок API — 100, больше игнорирует
JOBICY_REQUEST_CONCURRENCY = int(os.getenv('JOBICY_REQUEST_CONCURRENCY', '4'))
JOBICY_INDUSTRIES = [i.strip() for i in os.getenv('JOBICY_INDUSTRIES', ','.join((
    '', 'engineering', 'dev', 'data-science', 'devops-sysadmin', 'business',
    'product', 'design', 'technical-support', 'management', 'finance-legal',
))).split(',') if i.strip() or i == '']
# themuse.com — самый крупный из глобальных (407k в общей выдаче), ключ не нужен.
# ПОТОЛОК 99 страниц по 20 на запрос (страница 100 -> HTTP 400), поэтому охват набирается
# КОМБИНАЦИЯМИ category × level: каждая до 1980 записей. Серверные фильтры сужают сильно
# (SE 100 877, SE+Entry 30 348), но глубже 1980 по одной комбинации не достать.
# Сортировки по дате у API НЕТ — свежесть режется на нашей стороне, см. THEMUSE_MAX_AGE_DAYS.
THEMUSE_MAX_PAGES = int(os.getenv('THEMUSE_MAX_PAGES', '99'))        # жёсткий потолок портала
THEMUSE_PAGE_CONCURRENCY = int(os.getenv('THEMUSE_PAGE_CONCURRENCY', '6'))
# Порог свежести: 60 дн = граница гост-вакансии в домене (freshness.GHOST_DAYS). Старше —
# отклик всё равно бессмыслен, а в выдаче портала попадаются публикации годичной давности.
THEMUSE_MAX_AGE_DAYS = int(os.getenv('THEMUSE_MAX_AGE_DAYS', '60'))
# Комбинации фильтров. Уровни выше senior не берём — они всё равно в блеклисте отбора.
THEMUSE_QUERIES = [q.strip() for q in os.getenv('THEMUSE_QUERIES', ';'.join((
    'category=Software%20Engineering&level=Entry%20Level',
    'category=Software%20Engineering&level=Mid%20Level',
    'category=Data%20and%20Analytics&level=Entry%20Level',
    'category=Data%20and%20Analytics&level=Mid%20Level',
    'category=Data%20Science&level=Entry%20Level',
    'category=Data%20Science&level=Mid%20Level',
    'category=IT&level=Entry%20Level',
    'category=IT&level=Mid%20Level',
    'location=Flexible%20%2F%20Remote',
))).split(';') if q.strip()]
HH_ENRICH_BATCH_MULT = int(os.getenv('HH_ENRICH_BATCH_MULT', '4'))          # batch = CONCURRENCY * MULT

# ─── ATS работодателей (greenhouse, ashby) ────────────────────────────────────
# Не агрегаторы, а борды КОНКРЕТНЫХ компаний: один запрос = один работодатель. Отсюда
# главное отличие от всех остальных источников — охват задаёт РЕЕСТР НИЖЕ, а не портал.
# Замер 13.08.2026 по этим спискам: greenhouse 6393 вакансии с 32 бордов, ashby 2440 с 21.
#
# Почему реестр руками. API для перечисления компаний ни у одной из платформ нет: борд
# доступен только по слагу, и узнать слаг можно лишь с сайта работодателя. Из 74 проверенных
# слагов ответил 53 — остальные либо не на этом ATS, либо зовутся иначе. Список ведётся
# вручную и протухает, когда компания переезжает между платформами; адаптер поэтому обязан
# ЖАЛОВАТЬСЯ на долю молчащих бордов, а не тихо собирать меньше (см. ATS_DEAD_BOARDS_WARN).
GREENHOUSE_BOARDS = [b.strip() for b in os.getenv('GREENHOUSE_BOARDS', ','.join((
    'databricks', 'stripe', 'datadog', 'mongodb', 'cloudflare', 'canonical', 'brex',
    'samsara', 'elastic', 'remotecom', 'pinterest', 'gitlab', 'affirm', 'lyft', 'twilio',
    'coinbase', 'figma', 'flexport', 'reddit', 'grafanalabs', 'asana', 'robinhood',
    'instacart', 'gusto', 'vercel', 'duolingo', 'temporaltechnologies', 'discord',
    'dropbox', 'cockroachlabs', 'airtable', 'doximity',
))).split(',') if b.strip()]
ASHBY_BOARDS = [b.strip() for b in os.getenv('ASHBY_BOARDS', ','.join((
    'openai', 'harvey', 'elevenlabs', 'sierra', 'ramp', 'decagon', 'cursor', 'vanta',
    'replit', 'baseten', 'supabase', 'sardine', 'watershed', 'linear', 'modal', 'hex',
    'warp', 'posthog', 'railway', 'browserbase', 'neon',
))).split(',') if b.strip()]
ATS_BOARD_CONCURRENCY = int(os.getenv('ATS_BOARD_CONCURRENCY', '6'))   # бордов параллельно
# Таймаут запроса к борду — СВОЙ, много больше общего CURL_MAX_TIME = 25 с. Борд отдаётся
# ОДНИМ ответом на всю компанию, и у крупных это мегабайты: databricks 807 вакансий вместе
# с описаниями (`?content=true`), openai 731, stripe 566. В одиночном прогоне они укладывались
# в 25 с, а в общем — нет: 14.08.2026 ровно шесть самых тяжёлых бордов (databricks, stripe,
# datadog, cloudflare, openai, harvey) не ответили, и источники недобрали 1129 и 547 вакансий.
# Мелкие борды от большего таймаута не страдают: они отвечают за доли секунды, а ждать дольше
# приходится только тому, кто реально льёт мегабайты.
ATS_MAX_TIME = int(os.getenv('ATS_MAX_TIME', '120'))                   # сек на один борд
# Доля молчащих бордов, выше которой это уже не «компания сменила ATS», а поломка (сеть,
# бан, смена API). Ниже порога — INFO с перечнем, выше — WARNING: реестр из 53 имён,
# схлопнувшийся до десяти, обязан быть виден в логе, иначе усохший сбор выглядит успешным.
# Тот же класс дефекта, что молчаливое усечение обхода у himalayas (07.08.2026).
ATS_DEAD_BOARDS_WARN = float(os.getenv('ATS_DEAD_BOARDS_WARN', '0.3'))
# devitjobs.uk — британская IT-доска. Вся выдача ОДНИМ запросом (2058 записей на 13.08.2026),
# пагинации нет. Валюты в схеме нет вовсе — вилка годовая в фунтах, см. devitjobs.py::_salary.
DEVITJOBS_URL = os.getenv('DEVITJOBS_URL', 'https://devitjobs.uk/api/jobsLight')
# Свой таймаут вместо CURL_MAX_TIME (25 с): ответ ~4 МБ, в простое идёт 4-5 с, но запрос
# стартует на пике веерного этапа 1 сбора и с 25 с падал (06-10.09.2026), см. devitjobs.py.
DEVITJOBS_MAX_TIME = int(os.getenv('DEVITJOBS_MAX_TIME', '90'))              # сек на запрос

# ─── Нормализация зарплат ─────────────────────────────────────────────────────
NET_FROM_GROSS = 0.87            # net = gross − 13% НДФЛ
WORK_HOURS_PER_MONTH = 160       # часовая ставка -> месячная (hirify _to_monthly)
MONTHS_PER_YEAR = 12             # годовая -> месячная
HIRIFY_HOURLY_MAX_USD = 300      # USD-серединка < порога -> считаем ставку почасовой
HIRIFY_YEARLY_MIN_USD = 25_000   # USD-серединка > порога -> годовой

# Мин. размер выборки для репрезентативной медианы зарплаты (иначе срез не показываем).
MIN_SAMPLE_SALARY = 5            # язык/опыт по всем городам
MIN_SAMPLE_CITY_LANG = 3         # ячейка город×язык — мельче срез, ниже порог

# ─── Сервер / отклики ─────────────────────────────────────────────────────────
SERVE_PORT = int(os.getenv('SERVE_PORT', '8000'))              # порт локального сервера ленты
# Сколько вакансий ПОДРЯД без кнопки отклика считать блокировкой, а не архивом. Замер по
# 19 суткам лога: в здоровые сутки максимальная серия 8 и 21, при блокировке — 97, 150, 268.
APPLY_SKIP_STREAK_MAX = int(os.getenv('APPLY_SKIP_STREAK_MAX', '50'))
# Сколько ПОДРЯД идущих «клик был, подтверждения нет» (ApplyOutcome.UNCONFIRMED) считать
# упором в потолок HH. Серия 5 выбрана по замеру 23.09.2026: в здоровых прогонах таких
# отказов 0-1 (см. журнал acc2), а упор даёт 33 подряд — порог ловит упор за минуту, до
# того как прогон начнёт слать бот-сигналы в стену. Не путать с APPLY_SKIP_STREAK_MAX: тот
# калиброван под «нет кнопки отклика/архив» и по своей шкале (50) упор в окно не видит.
APPLY_UNCONFIRMED_STREAK_MAX = int(os.getenv('APPLY_UNCONFIRMED_STREAK_MAX', '5'))
HH_DAILY_APPLY_CAP = int(os.getenv('HH_DAILY_APPLY_CAP', '200'))  # потолок откликов в сутки (лимит HH)
# Потолок СКОЛЬЗЯЩИХ 24ч — эмпирическое правило HH, не документированное. Замер 23.09.2026 по
# журналу acc2 (152 отклика): 7 откликов прошли, пока база росла 42 -> 48, СЛЕДУЮЩАЯ попытка
# (база 48) отказана, и все 33 попытки подряд после неё — тоже, хотя вакансии были живые
# (проверено прогоном: та же вакансия откликнулась при базе 28). 22.09 все 37 отклика за сутки
# прошли, потому что окно не превышало 37. Отсюда потолок: HH перестаёт подтверждать отклики
# выше ~48 за 24ч независимо от суточной квоты (200). Цель прогона упирается и в окно тоже
# (autoclick::_apply_batch), иначе прогон идёт в стену: 23.09 это стоило 33 кликов впустую.
HH_APPLY_ROLLING_CAP = int(os.getenv('HH_APPLY_ROLLING_CAP', '45'))  # откликов за скользящие 24ч
# Блок-лист РАБОТОДАТЕЛЕЙ для откликов (RFC-004). Общий для всех аккаунтов, поэтому живёт в
# .env, а не в профиле резюме: два профиля не должны разъехаться по такому правилу.
# Разделитель `;` — запятая встречается в самих названиях («Ромашка, Медиа»). Совпадение
# по целому слову с нормализацией омоглифов: candidates.py::employer_blocked. Пусто — без блока.
APPLY_EMPLOYER_BLOCKLIST = [w.strip() for w in os.getenv('APPLY_EMPLOYER_BLOCKLIST', '').split(';')
                            if w.strip()]

# ─── LLM-классификатор намерения (chat_intent) через OpenRouter ────────────────
# LLM ТОЛЬКО классифицирует намерение вопроса бота (метка), НЕ генерирует ответ — факты
# берутся локально из resume_profile.json. Провайдеро-нейтрально: OpenRouter — OpenAI-
# совместимый шлюз, модель = строка INTENT_MODEL. По умолчанию ВЫКЛЮЧЕНО (opt-in, как
# cover mode='llm'): без INTENT_LLM=1 сеть не дёргается даже при наличии ключа, и движок
# работает на regex как раньше.
OPENROUTER_API_KEY = os.getenv('OPEN_ROUTER_API_KEY', '').strip()   # имя переменной — как в .env
OPENROUTER_BASE_URL = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')
INTENT_MODEL = os.getenv('INTENT_MODEL', 'deepseek/deepseek-v4-flash')
INTENT_TIMEOUT = float(os.getenv('INTENT_TIMEOUT', '6'))           # сек; таймаут -> fallback на regex
INTENT_ENABLED = os.getenv('INTENT_LLM', '').strip().lower() not in ('', '0', 'false', 'no')

# ─── LLM-переформулировка ответа (chat_rephrase) через OpenRouter — human-gated ──────
# LLM переформулирует УЖЕ ОДОБРЕННЫЙ факт под вопрос; факт выбирает КОД (etap-1/regex),
# LLM только меняет формулировку. Валидатор _grounded режет ново-токенные фабрикации, а
# любую ИЗМЕНЁННУЮ формулировку человек подтверждает [y/N] перед отправкой (без tty/крон ->
# уходит источник дословно). Дефолт ВЫКЛЮЧЕНО; truthy-set ЯВНЫЙ (в отличие от INTENT): любое
# иное значение (=off/=n) НЕ включает — чтобы «выключил» не оборачивалось «включил».
REPHRASE_MODEL = os.getenv('REPHRASE_MODEL', INTENT_MODEL)          # дефолт — тот же DeepSeek
REPHRASE_TIMEOUT = float(os.getenv('REPHRASE_TIMEOUT', '8'))        # сек; генерация дольше метки
REPHRASE_MAX_TOKENS = int(os.getenv('REPHRASE_MAX_TOKENS', '300'))  # генерация, не 30-токенная метка
REPHRASE_ENABLED = os.getenv('REPHRASE_LLM', '').strip().lower() in ('1', 'true', 'yes', 'on')

# ─── LLM-заполнение форм-опросников (form_fill) через OpenRouter ─────────────────────────
# LLM черновит ответ на поле анкеты ТОЛЬКО из фактов резюме. Дефолт ВЫКЛЮЧЕНО; truthy-set
# ЯВНЫЙ (=off/=n не включают).
# ВНИМАНИЕ, ЗДЕСЬ ЖМЁТСЯ SUBMIT. Раньше в этом комментарии стояло «SUBMIT жмёт ЧЕЛОВЕК сам
# в видимом окне, НЕ в кроне» — это устарело и вводило в заблуждение: `forms.py::try_autofill`
# отправляет анкету сам, а `cron/cron_apply.bat` выставляет FORMS_LLM=1, то есть путь боевой
# и безлюдный. Отклик работодателю НЕОБРАТИМ. Актуальный контракт — docs/rfc-003-form-fill.md
# (инвариант «шлём ТОЛЬКО при полноте») и docs/security.md (описание вакансии — чужой текст
# в промпте, защита от prompt injection лежит на денилисте `form_fill.py::_sanitize`).
FORM_MODEL = os.getenv('FORM_MODEL', INTENT_MODEL)
FORM_TIMEOUT = float(os.getenv('FORM_TIMEOUT', '8'))
FORM_MAX_TOKENS = int(os.getenv('FORM_MAX_TOKENS', '300'))
FORM_MAX_ANSWER_LEN = int(os.getenv('FORM_MAX_ANSWER_LEN', '1500'))   # жёсткий лимит длины ответа поля
# Анкеты — только основному аккаунту, НЕЗАВИСИМО от FORMS_LLM (RFC-004): их заполняют ответы
# из профиля и personal/resume.md основного, и второй аккаунт отправил бы работодателю чужое резюме.
FORMS_ENABLED = (os.getenv('FORMS_LLM', '').strip().lower() in ('1', 'true', 'yes', 'on')
                 and ACCOUNT.is_main)

# ─── Профиль резюме — ЕДИНЫЙ источник для autoclick-фильтра и resume.js-скоринга ──────
# Раньше «ядро» дублировалось (autoclick.APPLY_CORE/APPLY_EXPS ⇄ resume.js) и молча расходилось.
# Правь resume_profile.json в корне; feed.py инжектит это в feed-data.js для ленты.
_RESUME_DEFAULT = {"core": ["Python", "FastAPI"], "exp_ids": ["noExperience", "between1And3"]}
# У второго аккаунта своё резюме и свой отбор; наличие файла гарантирует resolve_account.
_RESUME_FILE = BASE_DIR / "resume_profile.json" if ACCOUNT.is_main else ACCOUNT_DIR / "resume_profile.json"
try:
    _rp = ({**_RESUME_DEFAULT, **json.loads(_RESUME_FILE.read_text(encoding="utf-8"))}
           if _RESUME_FILE.exists() else _RESUME_DEFAULT)
except (json.JSONDecodeError, OSError):
    _rp = _RESUME_DEFAULT
RESUME_CORE = _rp["core"]            # технологии-ядро (нужна хотя бы одна) — показ/отклик
RESUME_EXP_IDS = _rp["exp_ids"]      # допустимый опыт (raw id, см. EXP_LABELS)

# ─── Имя владельца ────────────────────────────────────────────────────────────────────
# Нужно только распознаванию персонализации: работодатели начинают письмо «<Имя>, здравствуйте»,
# и без снятия обращения один и тот же шаблон рассылки выглядит как уникальный текст
# (`chat_class.py::norm_text`, `chat_answer.py::_BOILERPLATE`). До 25.09.2026 имя владельца было
# ЗАХАРДКОЖЕНО в этих двух регулярках — то есть личные данные лежали в исходнике, а форк обязан
# был искать и править оба места. Теперь имя живёт в `resume_profile.json::first_name`
# (файл в .gitignore); ключа нет -> пустая строка, персонализация просто не снимается.
# Сравнение по целому слову и регистронезависимо; имя экранируется перед подстановкой в regex.
USER_FIRST_NAME = str(_rp.get("first_name") or "").strip()

# ─── Скоринг «% совпадения»: ЯРУСЫ СТЕКА и ЖЕЛАННОСТЬ РОЛИ ───────────────────────────
# Две РАЗНЫЕ оси, и смешивать их нельзя (разбор 14.08.2026):
#   * ярус стека отвечает «насколько я к этому ГОТОВ»;
#   * множитель роли — «насколько я туда ХОЧУ».
# Пока роль выставлялась по близости навыков, близость считалась дважды: у DevOps
# инфраструктурные техи и так набирают по оси стека, и добавочный высокий множитель
# поднимал направление, куда владелец профиля идти не собирается.
#
# Почему роль вообще нужна, хотя есть техи. Замер по кешу 14.08.2026: из 30 533 вакансий
# с Python 10 657 (35 %) — это Data/ML, Аналитик и Data Eng. По стеку они НЕ отличаются от
# бэкенда: среднее число попаданий в ядро у Data Eng 1.57 против 1.35 у «Разработчика» —
# там те же Kafka, ClickHouse и PostgreSQL. Сколько ни крути веса техов, дата-инженер
# будет обгонять бэкендера; различает только роль.
#
# Формат человеческий (списки имён), как у блеклистов: числа проставляет код, не человек.
_STACK_TIER_WEIGHTS = {"core": 1.0, "adjacent": 0.5, "background": 0.2}
_RESUME_STACK_DEFAULT = {
    # то, чем владею
    "core": ["Python", "FastAPI", "SQLAlchemy", "Alembic", "PostgreSQL", "Redis",
             "RabbitMQ", "Kafka", "ClickHouse", "MySQL", "Airflow", "Celery"],
    # рядом: разберусь по ходу. Django не трогал; RAG и LangChain — заявленное направление
    # развития (прикладной GenAI без претрейна и файнтюнинга), поэтому половина веса,
    # а не полный. 'ML/AI' сюда НЕ входит: этот тег накрывает и обучение моделей тоже.
    "adjacent": ["Django", "RAG", "LangChain"],
    # общая инженерная гигиена: есть почти у всех, сигнал слабый, но не нулевой
    "background": ["Docker", "Kubernetes", "Nginx", "AWS", "GCP", "Azure",
                   "Yandex Cloud", "GitLab CI", "GitHub Actions", "Terraform", "Ansible",
                   "MongoDB", "Elasticsearch", "SQLite", "MSSQL", "Oracle DB"],
}
# Ключ — ЯРЛЫК роли (Role.label). Согласованность с доменом держит страж
# tests/backend/domain/test_role.py: у каждой роли обязан быть множитель.
_RESUME_ROLE_FIT_DEFAULT = {
    "Backend": 1.0, "Разработчик": 1.0, "Architect": 1.0,
    "GenAI": 0.8,                    # желаемое развитие: RAG и гардрейлы, без претрейна
    "Fullstack": 0.6,                # подходит, но ниже GenAI; нехватку фронта снимет ось стека
    "DevOps": 0.3,                   # близко по навыкам, идти не хочу — близость уже в стеке
    "Data Eng": 0.3, "Data/ML": 0.3, "Аналитик": 0.3,
    "QA": 0.15, "Frontend": 0.15, "Mobile": 0.15, "Security": 0.15,
    "Embedded": 0.15, "Gamedev": 0.15, "Менеджер": 0.15, "Дизайнер": 0.15,
    "Не-IT": 0.0,
}


def _profile_map(key: str, default: dict[str, float]) -> dict[str, float]:
    """Числовая карта из профиля; ключа нет -> `default`. Чужой тип — предупреждение
    и дефолт (профиль пишет человек, ронять сбор из-за опечатки нельзя)."""
    raw = _rp.get(key)
    if raw is None:
        return default
    if not isinstance(raw, dict) or not all(
            isinstance(v, (int, float)) for v in raw.values()):
        log.warning("resume_profile.json: «{}» должен быть словарём «имя: число» — "
                    "беру значение по умолчанию", key)
        return default
    return {str(k): float(v) for k, v in raw.items()}


def _stack_tiers() -> dict[str, float]:
    """Ярусы -> плоская карта «тех: вес». Профиль задаёт СПИСКИ ИМЁН по ярусам, вес яруса
    известен коду: человек не должен подбирать числа, чтобы поправить свой стек."""
    src = _rp.get("stack_tiers")
    tiers = src if isinstance(src, dict) else _RESUME_STACK_DEFAULT
    out: dict[str, float] = {}
    for tier, weight in _STACK_TIER_WEIGHTS.items():
        for tech in (tiers.get(tier) or []):
            out[str(tech)] = weight
    return out or dict.fromkeys(RESUME_CORE, 1.0)


RESUME_STACK_TIERS = _stack_tiers()
RESUME_ROLE_FIT = _profile_map("role_fit", _RESUME_ROLE_FIT_DEFAULT)

# ─── ЯЗЫК — первая проверка, и она главнее стека ──────────────────────────────────────
# Порядок допущений (решение владельца профиля 14.08.2026): сначала ЯЗЫК, потом стек,
# потом роль. Без этой оси формула проваливалась грубо: «Ведущий разработчик 1С» с техами
# 1С/PostgreSQL/Kafka/RabbitMQ набирал 91 % — три ядровых попадания давали насыщение,
# а 1С весом 0 просто растворялся в знаменателе. Замер: 498 вакансий БЕЗ единого «своего»
# языка имели балл >= 70, среди них Go, Java, C++, Rust, PHP и Bitrix.
#
# Язык берётся из ЯКОРЯ профиля (`RESUME_CORE` ∩ `LANG_KEYS`), а не отдельной настройкой:
# ядро уже отвечает на вопрос «на чём я пишу» для жёсткого фильтра ленты, и второй источник
# того же ответа разъехался бы — ровно тот дефект, ради которого профиль и заводился.
# Само вычисление — НИЖЕ, сразу после LANG_KEYS (он объявлен в конце файла, рядом с
# TECH_PATTERNS): порядок в модуле важен, ссылаться на ещё не объявленное имя нельзя.
#
# Три исхода, и средний важен: «язык не назван» — это НЕ «язык чужой». У части вакансий
# стек в тексте не перечислен вовсе, и карать их наравне с Java-вакансией нельзя.
_RESUME_LANG_FIT_DEFAULT = {"own": 1.0, "none": 0.5, "foreign": 0.1}
RESUME_LANG_FIT = _profile_map("lang_fit", _RESUME_LANG_FIT_DEFAULT)
# Сколько попаданий в ядро считать полной вовлечённостью стека. Вакансия не перечисляет
# все восемь ядровых техов никогда: медиана попаданий по кешу — 1-2, поэтому насыщение
# на трёх. Выше порога добавка не растёт — иначе вернулась бы портальная многословность.
RESUME_CORE_SATURATION = int(os.getenv('RESUME_CORE_SATURATION', '3'))


def _profile_list(key: str, default: list[str]) -> list[str]:
    """Список строк из профиля; ключа нет -> `default`.

    ПУСТОЙ СПИСОК В ПРОФИЛЕ ЗНАЧИМ и в дефолт НЕ проваливается — тот же контракт, что у
    `blacklists`: пустое значение = настройка снята. Иначе «мне офис не нужен»
    (`"office_cities": []`) молча вернуло бы чужие города из дефолта.
    Профиль пишет человек, поэтому чужой тип — не падение, а предупреждение и дефолт."""
    raw = _rp.get(key)
    if raw is None:
        return default
    if not isinstance(raw, list):
        log.warning("resume_profile.{}: ожидался список, получено {} — беру дефолт",
                    key, type(raw).__name__)
        return default
    return [str(x) for x in raw]


def _profile_list_or_default(key: str, default: list[str]) -> list[str]:
    """Как `_profile_list`, но пустой список ТОЖЕ уходит в дефолт.

    Второй контракт нужен там, где пустое значение не настройка, а сломанный конфиг:
    `"search_queries": []` обнулило бы сбор целиком. Выбирается явно на месте вызова —
    угадывать по имени ключа, значим тут пустой список или нет, нельзя."""
    return _profile_list(key, default) or list(default)


# ── Чёрные списки отбора: ПЕРЕОПРЕДЕЛЯЮТСЯ профилем ──────────────────────────────────
# Это ПРЕДПОЧТЕНИЯ СОИСКАТЕЛЯ, а не логика приложения: «не беру QA» у одного и «беру только
# QA» у другого — одинаково законные настройки. До 07.08.2026 все восемь регексов были
# вшиты в candidates.py, и человеку с другим профилем пришлось бы править исходник, а его
# правки конфликтовали бы при каждом обновлении из апстрима.
#
# Значение — СПИСОК СЛОВ (`["QA", "тестировщ*"]`); хвост `*` = совпадение по началу, под
# русскую морфологию. Регулярку писать не нужно: её собирает `candidates._words_to_rx`.
# Строка тоже принимается и трактуется как готовый регекс — так записаны дефолты и так
# удобно тем, кому нужен lookaround. Ключ отсутствует в профиле -> берётся дефолт из
# candidates.py. Пустой список -> правило ВЫКЛЮЧЕНО: «"qa": []» означает «QA меня интересуют».
# Пример со всеми ключами — в resume_profile.example.json.
_bl_raw = _rp.get("blacklists")
APPLY_BLACKLISTS: dict[str, str | list[str]] = {
    str(k): ([str(x) for x in v] if isinstance(v, list) else v)
    for k, v in _bl_raw.items()
    if isinstance(v, (str, list)) and not str(k).startswith("_")  # _help-ключи — пояснения
} if isinstance(_bl_raw, dict) else {}                     # профиль пишет человек: не dict -> игнор
# Шаблон сопроводительного письма для КРОН-откликов (cover.py). У ленты свой набор —
# src/feed/cover.js::COVER_TEMPLATES; этот уходит с автооткликами.
RESUME_COVER_TEMPLATE: str = str(_rp.get("cover_template") or "").strip()
# Шаблоны писем ЛЕНТЫ (клик «письмо» в карточке). Отдельно от cover_template: у крона один
# текст, у ленты набор, из которого выбирается стабильный вариант по вакансии. Строки с
# подстановками {role} / {company} / {stack}. Пусто -> дефолты из src/feed/cover.js: «ни одного
# письма» — не настройка, а сломанная кнопка, поэтому здесь пустой список НЕ значим.
FEED_COVER_TEMPLATES: list[str] = [
    t for t in _profile_list("feed_cover_templates", []) if t.strip()]

# ─── Многоуровневый отбор откликов (autoclick), приоритет tier1 -> tier2 -> tier3 ──────
# tier1 — строгое ядро RESUME_CORE (Python/FastAPI), удалёнка; исчерпается — пойдёт tier2.
# tier2 — широкий стек, удалёнка. tier3 — любой из (строгий+широкий), ОФИС, только эти города.
# ВАЖНО про смысл tier3: это НЕ готовность к переезду. Кандидат ищет удалёнку; крупные города
# в списке потому, что там офисная по описанию вакансия с большой вероятностью допускает
# удалённый формат — то есть это ставка на переговоры, а не на релокацию.
# Порядок гарантируется сортировкой пула по tier: строгие уходят первыми, простоя между нет.
# ВСЕ ТРИ ТИРА живут в resume_profile.json (08.08.2026). Раньше tier1 был в профиле, а
# tier2/3 здесь — и это был помеченный компромисс: в `APPLY_OFFICE_CITIES` буквально стоял
# город владельца, то есть чужой форк обязан был править исходник. Дефолты ниже сохранены.
#
# `"office_cities": []` в профиле ВЫКЛЮЧАЕТ тир3 (см. `_profile_list` про пустые списки).
# Пустой набор безопасен: `techs & set()` и `city in set()` всегда ложны — тир не срабатывает.
_CORE_WIDE_DEFAULT = ["Django", "Flask", "PostgreSQL", "MySQL", "Redis", "Kafka"]
# НЕЙТРАЛЬНЫЙ дефолт (25.09.2026): здесь стоял город владельца, то есть чужой форк обязан был
# править исходник, а сам репозиторий раскрывал, где живёт пользователь. Теперь личные города
# живут в `resume_profile.json::office_cities` (файл в .gitignore), а дефолт — два крупнейших
# рынка. Профиль заменяет список ЦЕЛИКОМ: у кого свои города — просто указывает их у себя.
_OFFICE_CITIES_DEFAULT = ["Москва", "Санкт-Петербург"]
# Опыт, добавляемый к отбору СВЕРХ resume.exp_ids: вилку «3–6 лет» массово ставят на мидл-
# позиции, куда откликаться уместно, но задирать сам профиль нельзя — поедет процент матча.
_EXTRA_EXP_IDS_DEFAULT = ["between3And6"]

APPLY_CORE_WIDE = set(_profile_list("core_wide", _CORE_WIDE_DEFAULT))
APPLY_OFFICE_CITIES = set(_profile_list("office_cities", _OFFICE_CITIES_DEFAULT))
APPLY_EXTRA_EXP_IDS = _profile_list("extra_exp_ids", _EXTRA_EXP_IDS_DEFAULT)
# Принимать ГИБРИД (Schedule.HYBRID, «flexible») в ЛЮБОМ городе как удалёнку (RFC-004). Ключа
# нет -> False: у основного гибрид идёт офисной веткой (только APPLY_OFFICE_CITIES), поведение
# не меняется. Второй аккаунт ставит accept_hybrid:true — гибрид под наш стек в любом городе
# (частично из дома), и пул ниши заметно шире.
APPLY_ACCEPT_HYBRID = bool(_rp.get("accept_hybrid", False))
# Белый список ролей под автоотклик — ярлыки `Role.label` (RFC-004). Ключа нет -> None, то есть
# без ограничения: так живёт основной профиль. Второй аккаунт сужает отбор до своих ролей
# («Backend», «Разработчик», «GenAI»). Неизвестный ярлык валит импорт candidates.py (строгий
# разбор своей константы), а не молча выключает роль.
APPLY_ROLES: list[str] | None = (_profile_list("apply_roles", []) if "apply_roles" in _rp else None)
# Слова, одно из которых обязано стоять в тайтле вакансии с ролью «Разработчик» (RFC-004).
# Эта роль — фолбэк детектора: тайтл роль не назвал, а язык нашёлся в описании. У второго
# аккаунта так в пул шли «Senior Ceph Engineer» и «Marketing Analytics» (замер 15.09.2026: 8 из 12).
# Ключа нет -> None, проверки нет (основной профиль). Формат — как у blacklists: слова, `*` в конце.
APPLY_DEVELOPER_TITLE_WORDS: list[str] | None = (
    _profile_list("developer_title_words", []) if "developer_title_words" in _rp else None)

# ── Официальный API hh.ru (второй путь рядом с браузерным) ───────────────────────────
# Проверено 28.07: api.hh.ru НЕ закрыт DDoS-Guard, как считалось при написании
# `sources/hh.py`. Запрос доходит до самого HH (`Server-Timing: frontik`), и без токена
# приходит `{"errors":[{"value":"bad_authorization","type":"oauth"}]}` — то есть нужен
# OAuth-токен, а не обход защиты. Ключи — СВОЕГО приложения с dev.hh.ru.
HH_CLIENT_ID     = os.getenv('HH_CLIENT_ID', '').strip()
HH_CLIENT_SECRET = os.getenv('HH_CLIENT_SECRET', '').strip()
HH_REDIRECT_URI  = os.getenv('HH_REDIRECT_URI', 'http://localhost:8765/callback').strip()
# Контакт в HH-User-Agent обязателен по правилам API: по нему HH связывается при проблемах.
HH_API_UA        = os.getenv('HH_API_UA', 'hr-work-applicant/1.0').strip()
HH_TOKEN_FILE    = DATA_DIR / 'hh_token.json'      # access/refresh, gitignored вместе с data/
HH_ACCESS_TOKEN  = os.getenv('HH_ACCESS_TOKEN', '').strip()   # ручной override токена
# Цель — МАКСИМАЛЬНЫЙ охват IT-рынка. Из-за лимита MAX_PAGES*PER_PAGE (2000/город)
# широкий «разработчик» обрезается за порогом, поэтому держим и зонтичные термины,
# и точечные (язык/слой) — узкий запрос вытаскивает вакансии, не попавшие в топ-2000.
# Порядок задаёт владельца id при дедупе (первое вхождение), на union охвата не влияет.
_SEARCH_QUERIES_DEFAULT = [
    # --- зонтичные (высокий recall) ---
    # ВНИМАНИЕ: голый 'developer' убран — на HH он матчит девелопмент-недвижимость
    # и Business Development целиком (~60% не-IT: охрана труда, врачи, машинисты),
    # а его уникальный вклад (21959 эксклюзива) на ~95% мусор. IT покрывают зонтичные ниже.
    'разработчик', 'программист', 'software engineer',
    # --- языки ---
    'python разработчик', 'java разработчик', 'javascript разработчик',
    'typescript разработчик', 'golang разработчик', 'c# разработчик',
    'c++ разработчик', 'php разработчик', 'kotlin разработчик',
    'scala разработчик', 'ruby разработчик', 'rust разработчик',
    'swift разработчик', '1с программист',
    # --- слои ---
    'backend разработчик', 'frontend разработчик', 'fullstack разработчик',
    'python backend',
    # --- данные / ML ---
    'data engineer', 'data scientist', 'machine learning', 'аналитик данных',
    # --- инфра / QA ---
    'devops', 'sre', 'qa engineer', 'тестировщик',
    # --- мобильные ---
    'android разработчик', 'ios разработчик', 'mobile developer',
    # --- ниши ---
    'blockchain developer', 'gamedev', 'embedded разработчик',
]
# ЧТО ИСКАТЬ — предпочтение соискателя, а не логика: дизайнеру или тестировщику нужен
# другой набор, и никакой блеклист этого не исправит — вакансии просто не соберутся.
# Профиль ПОЛНОСТЬЮ заменяет список (не дополняет): половинчатое слияние дало бы набор,
# который человек не писал и не может предсказать.
# В отличие от `office_cities`, пустой список тут НЕ значим и падает в дефолт: «искать нечего»
# это не предпочтение, а сломанный конфиг, который обнулил бы сбор целиком.
SEARCH_QUERIES = _profile_list_or_default('search_queries', _SEARCH_QUERIES_DEFAULT)
PER_PAGE = 100
MAX_PAGES = 20          # 20 * 100 = 2000 вакансий на город
CONCURRENCY = int(os.getenv('HH_CONCURRENCY', '5'))   # параллельных запросов (env-override)
PAGE_DELAY = 0.25       # сек между страницами
# Пересобирать не чаще раза в день. Env-override нужен крону: при ежедневном запуске возраст
# кеша попадает ровно на границу 24ч, и сбор то срабатывал бы, то нет (cron_collect.bat = 20).
CACHE_TTL_HOURS = int(os.getenv('CACHE_TTL_HOURS', '24'))
# Возраст среза, с которого лента показывает баннер «данные устарели». НЕ равен TTL выше
# намеренно: TTL отвечает на «пора ли собирать», а этот порог — на «сбор перестал доезжать
# до кеша». Между ними обязан помещаться один пропущенный суточный прогон плюс запас на сон
# машины и догоняющий слот планировщика, иначе баннер загорался бы каждое утро на здоровой
# системе и его перестали бы замечать. 36ч = 24 (суточная сетка) + 12 (запас).
# Порог сторожит МОЛЧАЛИВЫЙ отказ: 16-19.08.2026 санити-гейт трое суток отменял запись кеша
# (himalayas отдавал 1913 против 26148 в базе), лента и дашборд каждый день пересобирались
# из замороженного среза и выглядели живыми, а отклики встали на второй день — пул кандидатов
# строится из того же кеша. Заметили вручную, по остановившемуся applied_log.jsonl.
STALE_CACHE_HOURS = int(os.getenv('STALE_CACHE_HOURS', '36'))

# ─── Dashboard palette ────────────────────────────────────────────────────────

BG     = "#0f1117"
PAPER  = "#1a1d27"
GRID   = "#2a2d3a"
TEXT   = "#e0e0e0"
ACCENT = "#4C78A8"
# Палитра свежести переехала в domain.freshness.FreshnessClass.color (единый источник
# кода/подписи/цвета/порядка) — раньше цвета жили здесь, а подписи в freshness.py,
# и словари молча расходились (у цветового не было ключа `unknown`).

# ─── Cities ───────────────────────────────────────────────────────────────────

_CITIES_DEFAULT = {
    # Россия
    '1':   'Москва',
    '2':   'Санкт-Петербург',
    '3':   'Екатеринбург',
    '4':   'Новосибирск',
    '88':  'Казань',
    '66':  'Нижний Новгород',
    '53':  'Краснодар',
    '78':  'Омск',
    '68':  'Самара',
    '76':  'Ростов-на-Дону',
    '54':  'Красноярск',
    '99':  'Уфа',
    '26':  'Воронеж',
    '72':  'Пермь',
    '212': 'Тольятти',
    '104': 'Челябинск',
    '95':  'Тюмень',
    '90':  'Томск',
    '24':  'Волгоград',
    '79':  'Саратов',
    '96':  'Ижевск',
    '112': 'Ярославль',
    '22':  'Владивосток',
    '11':  'Барнаул',
    '71':  'Пенза',
    '77':  'Рязань',
    '15':  'Астрахань',
    '47':  'Кемерово',
    # Беларусь
    '1002': 'Минск',
    '1003': 'Гомель',
    '1005': 'Витебск',
    '1007': 'Брест',
    '1006': 'Гродно',
    '1004': 'Могилёв',
}
# ГДЕ искать. Ключ — area id из справочника HH (api.hh.ru/areas), значение — подпись
# для отчётов. Профиль заменяет словарь целиком, по той же причине, что и запросы.
_cities_raw = _rp.get('cities')
# Профиль пишет человек: чужой тип -> предупреждение и дефолт (как в `_profile_list`).
# Пустой словарь тоже уходит в дефолт: сбор без городов не собрал бы вообще ничего.
if _cities_raw is not None and not isinstance(_cities_raw, dict):
    log.warning("resume_profile.cities: ожидался словарь area_id -> название, получено {} — "
                "беру дефолт", type(_cities_raw).__name__)
CITIES = ({str(k): str(v) for k, v in _cities_raw.items()}
          if isinstance(_cities_raw, dict) and _cities_raw else dict(_CITIES_DEFAULT))

# ─── Tech patterns ────────────────────────────────────────────────────────────

TECH_PATTERNS: dict[str, str] = {
    # Языки
    'Python':           r'\bpython\b',
    'JavaScript':       r'\bjavascript\b|\bjs\b',
    'TypeScript':       r'\btypescript\b',
    'Java':             r'\bjava\b(?!script)',
    # «go» — 2 буквы, ловит бренд «Яндекс Go» (курьеры/такси) и пр. шум. Требуем
    # dev-контекст рядом: golang | go перед developer/разраб/backend | <dev-слово> go.
    'Go':               (r'\bgolang\b'
                         r'|\bgo\b(?=\W{0,3}(?:developer|разраб|программист|engineer|backend|/\s*(?:php|python|java|node)))'
                         r'|(?:разраб\w*|программист|developer|engineer|backend|бэкенд|fullstack|стек\w*|язык\w*|знание|опыт|владение|\bна|\bin|using)\W{1,3}go\b'),
    'C++':              r'c\+\+|\bcpp\b',
    # «c#» БЕЗ хвостового \b: после '#' (не-словесный символ) перед пробелом/концом
    # строки границы слова нет, и '\bc#\b' не матчил «c# developer» вообще.
    'C#':               r'\bc#|\.net\b',
    'PHP':              r'\bphp\b',
    'Kotlin':           r'\bkotlin\b',
    'Swift':            r'\bswift\b',
    'Rust':             r'\brust\b',
    'Ruby':             r'\bruby\b',
    'Scala':            r'\bscala\b',
    '1С':               r'\b1с\b|\b1c\b',
    # Frontend
    'React':            r'\breact\b',
    'Vue':              r'\bvue\.?js\b|\bvue\b',
    'Angular':          r'\bangular\b',
    'Next.js':          r'\bnext\.?js\b',
    'Svelte':           r'\bsvelte\b',
    # Backend-фреймворки
    'Django':           r'\bdjango\b',
    'FastAPI':          r'\bfastapi\b',
    'Flask':            r'\bflask\b',
    'Spring':           r'\bspring\b',
    'Node.js':          r'\bnode\.?js\b',
    'Laravel':          r'\blaravel\b',
    'ASP.NET':          r'\basp\.net\b',
    'NestJS':           r'\bnest\.?js\b',
    # Базы данных
    'PostgreSQL':       r'\bpostgresql\b|\bpostgres\b',
    'MySQL':            r'\bmysql\b',
    'MongoDB':          r'\bmongodb\b',
    'Redis':            r'\bredis\b',
    'ClickHouse':       r'\bclickhouse\b',
    'Elasticsearch':    r'\belasticsearch\b|\belastic\b',
    'Oracle DB':        r'\boracle\b',
    'MSSQL':            r'\bmssql\b|sql server',
    'SQLite':           r'\bsqlite\b',
    # ORM и миграции: чаще всего идут в связке с Python-бэкендом, но своим тегом полезны —
    # без них «FastAPI + Postgres» и «FastAPI + Postgres + SQLAlchemy + Alembic» неразличимы,
    # хотя вторая вакансия про ту же работу говорит подробнее. Замер 14.08.2026: SQLAlchemy
    # 194 вакансии, Alembic 36, ни одной в «Не-IT» (алембик-перегонный куб не всплыл).
    'SQLAlchemy':       r'\bsqlalchemy\b|\bsql\s?alchemy\b',
    'Alembic':          r'\balembic\b',
    # DevOps / инфра
    'Docker':           r'\bdocker\b',
    'Kubernetes':       r'\bkubernetes\b|\bk8s\b',
    'Kafka':            r'\bkafka\b',
    'RabbitMQ':         r'\brabbitmq\b',
    # Очередь задач Python-бэкенда, соседка Redis/RabbitMQ. Опасение про овощ замером
    # не подтвердилось: 159 вакансий, «Не-IT» ноль — пищевые вакансии отсекаются раньше.
    'Celery':           r'\bcelery\b',
    # Оркестратор пайплайнов. ОСТОРОЖНО с трактовкой: навык питоновский, но 40 % пойманных —
    # это Data Eng (594 из 1487), то есть присутствие Airflow в вакансии сигналит про
    # DWH-работу. Разводить «умею» и «хочу» должен скоринг (ярус стека против множителя
    # роли, см. RESUME_ROLE_FIT), а не словарь. Опасение про «air flow» (вентиляция)
    # не подтвердилось: «Не-IT» 33 из 1487, 2 %.
    'Airflow':          r'\bairflow\b|\bapache\s+air\s?flow\b',
    'GitLab CI':        r'\bgitlab\b',
    'GitHub Actions':   r'\bgithub actions\b',
    'Ansible':          r'\bansible\b',
    'Terraform':        r'\bterraform\b',
    'Nginx':            r'\bnginx\b',
    # Облако
    'AWS':              r'\baws\b',
    'GCP':              r'\bgcp\b|google cloud',
    'Azure':            r'\bazure\b',
    'Yandex Cloud':     r'\byandex cloud\b|яндекс.?облако',
    # ML / AI
    'ML/AI':            r'machine learning|\bml\b|deep learning|tensorflow|pytorch|\bllm\b|нейросет',
    # Прикладной GenAI-инструментарий. Отдельно от 'ML/AI' намеренно: тот мешок на 13 690
    # вакансий не различает обучение моделей и построение продукта поверх них, а разница
    # ровно в этом (см. границу ролей GenAI/Data-ML). Замер 14.08.2026: LangChain 372
    # вакансии, RAG 880, «Не-IT» ~1 %.
    # `\brag\b` ТОЛЬКО с границами слова: без них подстрока сидит в drag/storage/фрагмент.
    'LangChain':        r'\blangchain\b|\blang\s?chain\b|\bllamaindex\b|\bllama\s?index\b',
    'RAG':              r'\brag\b|retrieval.augmented',
    # Web3 / Blockchain
    'Web3/Blockchain':  r'web3|web 3\.0|blockchain|solidity|\bdefi\b|smart contract|\bcrypto\b|\bnft\b',
    # Мобайл
    'Android':          r'\bandroid\b',
    'iOS':              r'\bios\b',
    'Flutter':          r'\bflutter\b',
    'React Native':     r'\breact native\b',
}

# Только языки программирования — для зарплатного анализа
LANG_KEYS = {
    'Python', 'JavaScript', 'TypeScript', 'Java', 'Go',
    'C++', 'C#', 'PHP', 'Kotlin', 'Swift', 'Rust', 'Ruby', 'Scala', '1С',
}

# Мои языки для скоринга — пересечение якоря профиля с набором выше. Объявлено ЗДЕСЬ,
# а не рядом с остальными RESUME_*, потому что LANG_KEYS определяется только сейчас.
# Обоснование самой оси — у `RESUME_LANG_FIT` выше.
RESUME_LANGS = [t for t in RESUME_CORE if t in LANG_KEYS]

# Роль вакансии по ТАЙТЛУ (порядок важен — первое совпадение). Классифицирует
# «безъязыковые» (аналитик/QA/devops/…) и отделяет не-IT. Гейт: вакансия считается
# IT, если есть тех-тег ИЛИ тайтл матчит роль; иначе role='Не-IT' (фильтруется).
# Сниппет НЕ используем — тело JD упоминает «тестирование/безопасность/ML» и шумит.
ROLE_PATTERNS: dict[str, str] = {
    'Mobile':       r'\bandroid\b|\bios\b|flutter|react native|мобильн\w*\s*разраб|\bkmm\b',
    'QA':           r'\bqa\b|тестировщик|тестирован|\bтест\b|автотест|\baqa\b|quality assurance|\bsdet\b',
    'DevOps':       r'devops|\bsre\b|систем\w*\s*администратор|sysadmin|инфраструктур|cloud engineer|reliability|\bdba\b|администратор баз|сетев\w*\s*инженер|'
                    # БЕЗ голых `kubernetes` и `platform engineer`: DevOps стоит в таблице
                    # раньше Data Eng/Data-ML/Security, и такие широкие слова крали у них
                    # роль («Data Platform Engineer» -> DevOps, «Python-разработчик
                    # (Kubernetes)» -> DevOps). Замер: 161 + 51 + 26 ложных перекладок.
                    # devsecops — в Security, а НЕ здесь: DevOps идёт раньше, и 24 вакансии
                    # с явным «безопасная разработка/AppSec» перестали быть Security.
                    # `\biaas\b` не берём: IaaS — это домен продукта, а не роль
                    # («Старший Go-разработчик, IaaS» — backend). Нужный
                    # «IaaS / Kubernetes Platform Engineer» ловится по `kubernetes platform`.
                    r'system administrator|database engineer|'
                    r'kubernetes platform|windows server|'
                    r'администратор\s+(?:sap|opensource|open source)|'
                    r'инженер\w*\s*по\s*мониторингу|'
                    # ТОЛЬКО связка «инженер Linux», не голое `linux`. Замер 14.08.2026:
                    # слово встречается в 404 тайтлах, из них 52 — Embedded («Разработчик
                    # встраиваемого ПО Linux C/C++») и 93 — разработчики. DevOps стоит
                    # в таблице раньше Embedded и украл бы их все: ОС — предметная область,
                    # а не профессия (та же ловушка, что «AI-first» у роли GenAI).
                    r'инженер\w*\s+linux\b|linux[\s\-]?инженер',
    # Слова хранилищ добавлены 14.08.2026: «Ведущий разработчик Clickhouse» падал в общий
    # фолбэк «Разработчик» с множителем 1.0 и набирал 95 % — маркеров Data Eng в тайтле нет,
    # хотя работа заведомо DWH. Замер: безусловные DWH-слова переносят 74 вакансии
    # («Разработчик хранилищ данных», «Data Warehouse Architect»), ложных среди них не видно.
    #
    # `clickhouse` — под ЗАЩИТОЙ от бэкенд-тайтлов: сама по себе аналитическая СУБД роль
    # не определяет («Backend Engineer (ClickHouse)» и «Software Engineer (Platform/Backend)
    # (ClickHouse)» — честные бэкенды, замер нашёл ровно их).
    # `инженер\w*` вместо `инженер`: «Руководитель отдела инженерИИ данных» мимо точной
    # формы не проходил и уезжал в общий фолбэк «Разработчик» с множителем 1.0 — 100 %
    # у начальника DWH-отдела (замер 14.08.2026).
    'Data Eng':     r'data engineer|инженер\w*\s+данных|дата.?инженер|\betl\b|data platform|\bdwh\b|'
                    r'хранилищ\w*\s+данных|data\s+warehouse|витрин\w*\s+данных|\bgreenplum\b|'
                    # `data[\s\-.]?инженер` закрывает дыру между кириллическим `дата-инженер`
                    # и латинским `data engineer` (с пробелом): форма «Data-инженер» через
                    # дефис проваливалась между ними. Замер: 2618 тайтлов, 2586 уже Data Eng —
                    # паттерн лишь подтверждает то, что и так распознано.
                    r'data[\s\-.]?инженер|'
                    # BigData. Замер: 195 тайтлов; переезжают в основном из общего фолбэка
                    # (49) — «Разработчик BigData», «Senior Big Data Developer (Apache Spark)».
                    # Роли Аналитик/Data-ML не задеты по существу: у них тот же множитель.
                    r'\bbig[\s\-]?data\b|бигдата|'
                    r'^(?!.*(?:backend|бэкенд|бекенд|platform|платформ)).*\bclickhouse\b',
    # Английские ML-сокращения (getmatch/Сбер-стиль тайтлов). MLE/LLM/VLM однозначны;
    # RL и DL — ТОЛЬКО в связке с engineer/инженер/lead: двухбуквенный код сам по себе
    # ловил бы случайные подстроки.
    # GenAI стоит ПЕРЕД Data/ML, и это единственное, что задаёт границу между ними:
    # выигрывает первое совпадение. Вынимать генеративные токены ИЗ Data/ML при этом
    # НЕЛЬЗЯ — попробовали 14.08.2026, и `\bai\b` держал в Data/ML около семи тысяч
    # тайтлов, которые узкий GenAI-регекс не ловит: без него 3520 из них проваливались
    # в Не-IT, то есть выбрасывались бы прямо на сборе у восьми не-РФ источников.
    # Оставленный дубль (`genai`/`llm`/`ai` есть и здесь, и ниже) безвреден ровно потому,
    # что порядок разбирает спор: до Data/ML доходит только то, что GenAI не взял.
    #
    # Голое `ai` сюда НЕ входит намеренно: «AI-first», «AI-Powered», «AI-продукт» — это
    # описание продукта, а не профессия. Замер: широкий вариант забирал 10 712 вакансий
    # и воровал роль у Mobile (92), QA (191) и DevOps (97). Берём `ai` только в связке
    # с названием профессии (engineer/developer/инженер/разработчик).
    #
    # `\bпромт\b` — с границами слова обязательно: без них подстрока сидит внутри
    # «Мин-промт-оргом», и «Специалист по работе с Минпромторгом» уезжал в GenAI.
    #
    # ГРАНИЦА С Data/ML: сюда идёт ПРИКЛАДНАЯ работа на генеративных моделях (RAG, агенты,
    # промпты, AI-инженер), а ОБУЧЕНИЕ моделей остаётся в Data/ML. Претрейн, пост-тренинг
    # и RLHF — классический ML, просто модель генеративная; «Senior Research Engineer
    # (LLM Pretraining)» ближе к дата-сайентисту, чем к тому, кто собирает RAG-пайплайн.
    # Решение владельца профиля 14.08.2026, цена вопроса — 65 вакансий.
    #
    # Исключение выражено ОПЕРЕЖАЮЩЕЙ ПРОВЕРКОЙ, а не порядком таблицы, и иначе не выходит:
    # GenAI обязан стоять раньше Data/ML (см. выше), поэтому «сначала обучение, потом
    # генератив» порядком не записать — только внутри паттерна.
    #
    # Замер 14.08.2026 (131 804 тайтла): роль набирает 1962, посторонних перекладок 0,
    # 22 вакансии вытащены из Не-IT («Специалист по генеративному контенту»,
    # «MLOPS/LLMops инженер») — их до этого выбрасывал отсев не-IT на глобальных источниках.
    # `\bvlm\b` и `\bdl\b` стоят в маркерах ОБУЧЕНИЯ, а не в генеративных: обе аббревиатуры
    # встречаются у исследователей моделей («Senior DL (VLM, GigaChat Vision)»).
    'GenAI':        r'^(?!.*(?:pretrain|pre.?train|post.?train|\bmle\b|research\s+engineer|'
                    r'\brlhf\b|fine.?tun|дообучен|обучени\w*\s+(?:модел|нейросет)|'
                    r'\bvlm\b|\bdl\b))'
                    r'(?:.*(?:genai|\bgen\s*-?\s*ai\b|generative|генератив|\bllm\b|\bllms\b|'
                    r'llmops|large language model|языков\w*\s+модел|prompt\s+engineer|'
                    r'\bпромпт|\bпромт\b|\brag\b|retrieval.augmented|ai.?agent|мультиагентн|'
                    r'агентн\w*\s+(?:систем|ai|ии)|'
                    r'\bai[\s\-/]*(?:engineer|developer|разработчик|инженер)|'
                    r'(?:инженер|разработчик)\w*[\s\-/]*\bai\b|ии.?разработчик|ai.?инженер))',
    'Data/ML':      r'data scien|machine learning|\bml\b|\bnlp\b|нейросет|computer vision|дата.?са\w*|ml.?eng|\bai\b|искусствен\w*\s*интеллект|ml.?разраб|'
                    r'\bmle\b|\bllm\b|\bvlm\b|deep learning|\bcuda\b|нейронн\w*\s*сет\w*|'
                    r'research engineer|pretrain|post.?training|genai|'
                    r'\b(?:rl|dl)[\s\-]?(?:engineer|инженер|lead)',
    # `дашборд` — ТОЛЬКО кириллицей. Замер 14.08.2026: латинское `dashboard` сидит
    # в тайтлах фронтендеров («Dashboard Experience Developer», «Staff Software Engineer,
    # Dashboard») — это интерфейс, а не BI. Русская форма встречается у отчётности:
    # «Разработчик Дашбордов» получал 90 % совпадения как общий разработчик.
    'Аналитик':     r'систем\w*\s*аналит|бизнес.?аналит|data analyst|аналитик данных|bi.?аналит|продуктов\w*\s*аналит|аналитик 1с|\bbi\b|финанс\w*\s*аналит|аналитик|\banalyst\b|'
                    r'дашборд|аналитическ\w+\s+(?:решени|систем)',
    'Embedded':     r'embedded|встраиваем|firmware|схемотехник|\bfpga\b|\basic\b|микроконтроллер|\bплис\b|verilog|радиоэлектрон|разработчик электрон',
    # infrasec — НЕ опечатка infosec (инфраструктурная безопасность), DLP/EDR/СЗИ — классы
    # средств защиты: тайтл «Администратор средств защиты от утечек (DLP)» слова
    # «безопасность» не содержит вовсе.
    'Security':     r'безопасн|security|пентест|pentest|infosec|appsec|soc analyst|защит\w*\s*информ|'
                    r'infrasec|penetration test|\bdlp\b|\bedr\b|\bсзи\b|защит\w*\s*от\s*утечек|'
                    r'devsecops',
    'Gamedev':      r'gamedev|game dev|\bunity\b|unreal|геймдиз|game.?prog|game.?desi',
    'Architect':    r'архитектор|architect',
    'Дизайнер':     r'дизайнер|designer|\bux\b|ui/ux|product design',
    'Frontend':     r'frontend|front-end|фронт.?енд|фронтенд|верстальщик|\bvue\b|angular|\breact\b(?! native)',
    'Backend':      r'backend|back-end|бэкенд|сервер\w*\s*разраб',
    'Fullstack':    r'fullstack|full.?stack|фулстек|фуллстек',
    'Менеджер':     r'продакт|product manager|проджект|project manager|тимлид|team.?lead|руководител\w*\s*(разраб|групп|отдел\w*\s*разраб|\bit\b|ит)|head of (eng|dev|data)|\bcto\b|scrum master|владелец продукта|product owner|'
                    r'менеджер\w*\s*продукта|engineering manager|tech.?lead|техлид|delivery manager|'
                    r'head of product|\bcpo\b',
    'Разработчик':  r'разработчик|программист|\bdeveloper\b|инженер.?программист|software eng\w*|\bdev\b|'
                    r'staff engineer|principal engineer',
    # Роли Support здесь НЕТ намеренно: поддержка отсекается раньше, в
    # parsing._HARD_NON_IT — см. решение по инциденту «Яндекс Крауд: Поддержка».
}

EXP_LABELS = {
    'noExperience': 'Без опыта',
    'between1And3': '1–3 года',
    'between3And6': '3–6 лет',
    'moreThan6':    '6+ лет',
}

# Единый литерал состояния «опыт не назван». До 23.09.2026 он жил двумя копиями:
# `views/feed.py::EXP_UNKNOWN` (подпись пятого чипа фильтра) и `analyzer.py::salary_by_experience`
# (строка отчёта «6. Зарплата по опыту»). Чип и строка отчёта называют ОДНО состояние, и связать
# их было нечем: при расхождении пользователь не сопоставил бы фильтр с отчётом
# (аудит `2026-09-22-quality.md`, §3.2). Код состояния — пустая строка, см. `EXP_UNKNOWN`.
EXP_UNKNOWN_LABEL = 'Не указан'


def _known_exp_ids(key: str, ids: list[str]) -> list[str]:
    """Коды опыта из профиля, оставленные только известные (ключи `EXP_LABELS`).

    Опечатка в `resume_profile.json` обязана быть СЛЫШНА. До 08.08.2026 её выдавал KeyError
    при сборке ленты (подписи брались по коду), но после удаления моста `RESUME_EXPS_PY`
    (аудит, находки 22/30) неизвестный код перестал проявляться ГДЕ-ЛИБО: он просто ни с чем
    не совпадал и молча сужал и процент матча в ленте, и отбор под отклик — то есть
    недоотклики без единого признака в логе.

    ПОЧЕМУ ПРЕДУПРЕЖДЕНИЕ, А НЕ ПАДЕНИЕ — осознанный компромисс (docs/config.md). Правило
    проекта «fail fast на своих ошибках» относится к НАШИМ константам; `exp_ids` пишет
    ЧЕЛОВЕК в своём профиле, и весь файл читается по контракту `_profile_list`: чужой тип и
    кривое значение дают предупреждение и дефолт, а не обрыв. Плюс `config` импортируется
    КАЖДОЙ командой: исключение здесь уронило бы и сбор, и отчёты из-за опечатки в настройке
    отбора. Строгий разбор (`Experience.from_label`) остаётся там, где константу пишем мы.
    """
    bad = [e for e in ids if e not in EXP_LABELS]
    if bad:
        log.warning("resume_profile.{}: неизвестный код опыта {} — допустимы {}; "
                    "код исключён из отбора", key, bad, sorted(EXP_LABELS))
    return [e for e in ids if e in EXP_LABELS]


# Проверка стоит ЗДЕСЬ, а не рядом с присваиванием: критерий валидности — ключи `EXP_LABELS`,
# а словарь определён ниже по файлу. Настоящий источник кодов — `domain/experience.py`,
# но config импортируется доменом, и обратный импорт замкнул бы цикл.
RESUME_EXP_IDS = _known_exp_ids('exp_ids', RESUME_EXP_IDS)
APPLY_EXTRA_EXP_IDS = _known_exp_ids('extra_exp_ids', APPLY_EXTRA_EXP_IDS)
