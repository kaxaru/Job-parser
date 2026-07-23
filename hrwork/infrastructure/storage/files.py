"""Файловое хранилище: сырые данные и метаданные кеша."""
import datetime
import json
import time

from hrwork.config import (
    CACHE_TTL_HOURS,
    CITIES,
    DESC_CACHE_MAX_AGE_DAYS,
    META_FILE,
    RAW_FILE,
    SEARCH_QUERIES,
    SOURCES,
    log,
)
from hrwork.domain import freshness

from .jsonio import atomic_write_json


def now_iso() -> str:
    """Текущий момент в ISO-UTC — метка фактической дозагрузки описания (_enriched_at)."""
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def load_raw() -> list[dict]:
    with open(RAW_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_raw(items: list[dict]):
    # Атомарно (tmp + os.replace): крэш посреди записи не портит единственный файл
    # многочасового сбора.
    atomic_write_json(RAW_FILE, items)
    save_meta(len(items))


def load_desc_cache() -> dict[str, dict]:
    """Кеш описаний из прошлого сбора: {id: {sig, description_html, requirement}}.
    Только реально обогащённые записи (_enriched) — tldr-заглушки не должны блокировать
    будущую дозагрузку. sig — маркер изменения (hirify: updated_at; hh: published_at).
    requirement нужен HH: enrich заполняет и текст карточки (питает _detect_techs/_detect_role)."""
    if not RAW_FILE.exists():
        return {}
    try:
        prior = load_raw()
    except Exception as e:                       # битый/пустой кеш -> просто нет инкремента
        log.warning('Кеш описаний не прочитан ({}) — enrich с нуля.', e)
        return {}
    return {it['id']: {'sig': it.get('_sig') or '',
                       'description_html': it['description_html'],
                       'requirement': (it.get('snippet') or {}).get('requirement', ''),
                       'at': it.get('_enriched_at')}      # когда реально дозагружено (для max-age)
            for it in prior
            if it.get('_enriched') and it.get('description_html')}


def cache_hit_usable(hit: dict | None, cur_sig: str) -> bool:
    """Годна ли запись кеша: сигнал совпал, описание есть и не старше DESC_CACHE_MAX_AGE_DAYS
    (предохранитель от тихой правки без смены сигнала). 0 -> без ограничения по времени."""
    if not hit or hit['sig'] != cur_sig or not hit['description_html']:
        return False
    if DESC_CACHE_MAX_AGE_DAYS and hit.get('at'):
        age = freshness.age_days(hit['at'])
        if age is not None and age >= DESC_CACHE_MAX_AGE_DAYS:
            return False                                  # протухла по времени -> пере-обогатить
    return True


def cache_valid(force: bool = False) -> bool:
    """True, если кеш свежий и пересбор не нужен."""
    if force:
        return False
    if not RAW_FILE.exists() or not META_FILE.exists():
        return False
    try:
        with open(META_FILE, encoding='utf-8') as f:
            meta = json.load(f)
        if set(meta.get('cities', [])) != set(CITIES.keys()):
            log.info('Города изменились — пересбор данных.')
            return False
        if meta.get('queries', []) != SEARCH_QUERIES:
            log.info('Запросы изменились — пересбор данных.')
            return False
        if set(meta.get('sources', ['hh'])) != set(SOURCES):
            log.info('Набор порталов изменился — пересбор данных.')
            return False
        collected_at = meta.get('collected_at', 0)
        age_hours = (time.time() - collected_at) / 3600
        if age_hours >= CACHE_TTL_HOURS:
            log.info('Кеш устарел ({:.1f}ч >= {}ч) — пересбор данных.', age_hours, CACHE_TTL_HOURS)
            return False
        log.info('Используем кеш ({:.1f}ч, {} вакансий). Для пересбора: python hh.py collect --force',
                 age_hours, meta.get('count', '?'))
        return True
    except Exception as e:
        log.warning('Метаданные кеша не читаются ({}) — пересбор данных.', e)
        return False


def save_meta(count: int):
    meta = {
        'collected_at': time.time(),
        'cities': list(CITIES.keys()),
        'queries': SEARCH_QUERIES,
        'sources': SOURCES,
        'count': count,
    }
    atomic_write_json(META_FILE, meta, indent=2)
