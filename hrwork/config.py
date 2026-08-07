"""Все настройки и константы пакета hrwork."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# Логгер пакета: весь hrwork берёт его как `from hrwork.config import log`. Присваивание, а не
# `import logger as log`, — чтобы это был настоящий атрибут модуля, а не неявный реэкспорт
# (mypy --strict, no_implicit_reexport: иначе 24 ошибки во всех импортирующих модулях).
log = logger

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).resolve().parent.parent   # hr_work/

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

# ─── API / collection settings ────────────────────────────────────────────────

# Мульти-портальный агрегатор: какие источники собираем (реестр в data/sources.py).
# Порядок задаёт владельца id при дедупе (первое вхождение). hh — основной (с автооткликом),
# hirify — доп. портал (JSON-API, только просмотр/аналитика).
SOURCES = [s.strip() for s in os.getenv(
    'SOURCES',
    'hh,hirify,talanto,getmatch,arbeitnow,himalayas,web3,themuse,jobicy',
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
# порталу, а не только по стеку. Замер 07.08.2026: 17 тегов -> 957 уникальных вакансий.
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
HH_DAILY_APPLY_CAP = int(os.getenv('HH_DAILY_APPLY_CAP', '200'))  # потолок откликов в сутки (лимит HH)

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

# ─── LLM-заполнение форм-опросников (form_fill) через OpenRouter — human-fills-submit ────
# LLM черновит ответ на поле анкеты ТОЛЬКО из фактов резюме; тул ЗАПОЛНЯЕТ поля, но SUBMIT
# жмёт ЧЕЛОВЕК сам в видимом окне (тул submit не кликает — RFC-003). Дефолт ВЫКЛЮЧЕНО;
# truthy-set ЯВНЫЙ (=off/=n не включают). НЕ в кроне. См. docs/security.md.
FORM_MODEL = os.getenv('FORM_MODEL', INTENT_MODEL)
FORM_TIMEOUT = float(os.getenv('FORM_TIMEOUT', '8'))
FORM_MAX_TOKENS = int(os.getenv('FORM_MAX_TOKENS', '300'))
FORM_MAX_ANSWER_LEN = int(os.getenv('FORM_MAX_ANSWER_LEN', '1500'))   # жёсткий лимит длины ответа поля
FORMS_ENABLED = os.getenv('FORMS_LLM', '').strip().lower() in ('1', 'true', 'yes', 'on')

# ─── Профиль резюме — ЕДИНЫЙ источник для autoclick-фильтра и resume.js-скоринга ──────
# Раньше «ядро» дублировалось (autoclick.APPLY_CORE/APPLY_EXPS ⇄ resume.js) и молча расходилось.
# Правь resume_profile.json в корне; feed.py инжектит это в feed-data.js для ленты.
_RESUME_DEFAULT = {"core": ["Python", "FastAPI"], "exp_ids": ["noExperience", "between1And3"]}
_RESUME_FILE = BASE_DIR / "resume_profile.json"
try:
    _rp = ({**_RESUME_DEFAULT, **json.loads(_RESUME_FILE.read_text(encoding="utf-8"))}
           if _RESUME_FILE.exists() else _RESUME_DEFAULT)
except (json.JSONDecodeError, OSError):
    _rp = _RESUME_DEFAULT
RESUME_CORE = _rp["core"]            # технологии-ядро (нужна хотя бы одна) — показ/отклик
RESUME_EXP_IDS = _rp["exp_ids"]      # допустимый опыт (raw id, см. EXP_LABELS)


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
# Значение — СТРОКА-РЕГЕКС (как `q` в form_answers), собирается через `|`. Ключ отсутствует
# в профиле -> берётся дефолт из candidates.py. Пустая строка -> правило ВЫКЛЮЧЕНО:
# «"qa": ""» означает «QA меня интересуют».
# Пример со всеми ключами — в resume_profile.example.json.
_bl_raw = _rp.get("blacklists")
APPLY_BLACKLISTS: dict[str, str] = {
    str(k): v for k, v in _bl_raw.items()
    if isinstance(v, str) and not str(k).startswith("_")   # _help-ключи — пояснения, не правила
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
# ВАЖНО про смысл tier3: это НЕ готовность к переезду. Кандидат живёт в Тольятти и ищет
# удалёнку; крупные города в списке потому, что там офисная по описанию вакансия с большой
# вероятностью допускает удалённый формат — то есть это ставка на переговоры, а не на релокацию.
# Порядок гарантируется сортировкой пула по tier: строгие уходят первыми, простоя между нет.
# ВСЕ ТРИ ТИРА живут в resume_profile.json (08.08.2026). Раньше tier1 был в профиле, а
# tier2/3 здесь — и это был помеченный компромисс: в `APPLY_OFFICE_CITIES` буквально стоял
# город владельца, то есть чужой форк обязан был править исходник. Дефолты ниже сохранены.
#
# `"office_cities": []` в профиле ВЫКЛЮЧАЕТ тир3 (см. `_profile_list` про пустые списки).
# Пустой набор безопасен: `techs & set()` и `city in set()` всегда ложны — тир не срабатывает.
_CORE_WIDE_DEFAULT = ["Django", "Flask", "PostgreSQL", "MySQL", "Redis", "Kafka"]
_OFFICE_CITIES_DEFAULT = ["Москва", "Санкт-Петербург", "Тольятти", "Самара"]
# Опыт, добавляемый к отбору СВЕРХ resume.exp_ids: вилку «3–6 лет» массово ставят на мидл-
# позиции, куда откликаться уместно, но задирать сам профиль нельзя — поедет процент матча.
_EXTRA_EXP_IDS_DEFAULT = ["between3And6"]

APPLY_CORE_WIDE = set(_profile_list("core_wide", _CORE_WIDE_DEFAULT))
APPLY_OFFICE_CITIES = set(_profile_list("office_cities", _OFFICE_CITIES_DEFAULT))
APPLY_EXTRA_EXP_IDS = _profile_list("extra_exp_ids", _EXTRA_EXP_IDS_DEFAULT)

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
    # DevOps / инфра
    'Docker':           r'\bdocker\b',
    'Kubernetes':       r'\bkubernetes\b|\bk8s\b',
    'Kafka':            r'\bkafka\b',
    'RabbitMQ':         r'\brabbitmq\b',
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
                    r'инженер\w*\s*по\s*мониторингу',
    'Data Eng':     r'data engineer|инженер данных|дата.?инженер|\betl\b|data platform|\bdwh\b',
    # Английские ML-сокращения (getmatch/Сбер-стиль тайтлов). MLE/LLM/VLM однозначны;
    # RL и DL — ТОЛЬКО в связке с engineer/инженер/lead: двухбуквенный код сам по себе
    # ловил бы случайные подстроки.
    'Data/ML':      r'data scien|machine learning|\bml\b|\bnlp\b|нейросет|computer vision|дата.?са\w*|ml.?eng|\bai\b|искусствен\w*\s*интеллект|ml.?разраб|'
                    r'\bmle\b|\bllm\b|\bvlm\b|deep learning|\bcuda\b|нейронн\w*\s*сет\w*|'
                    r'research engineer|pretrain|post.?training|genai|'
                    r'\b(?:rl|dl)[\s\-]?(?:engineer|инженер|lead)',
    'Аналитик':     r'систем\w*\s*аналит|бизнес.?аналит|data analyst|аналитик данных|bi.?аналит|продуктов\w*\s*аналит|аналитик 1с|\bbi\b|финанс\w*\s*аналит|аналитик|\banalyst\b',
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
