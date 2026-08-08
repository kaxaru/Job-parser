"""Файловое хранилище: сырые данные и метаданные кеша."""
import datetime
import json
import time
from collections.abc import Mapping
from typing import Any, TypeGuard

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

from .jsonio import atomic_write_json, read_json_or

#: Ключ `cache_meta.json` с объёмами источников ДО кросс-портального дедупа — база сравнения
#: санити-гейта (`hh.py::_degraded_source`). Пишется в конце удачного сбора, читается в начале
#: следующего: сегодняшний `by_src` тоже считается ДО дедупа, и сравнивать надо одноимённое
#: с одноимённым, иначе порог мягче заявленного (аудит 08.08.2026, находка 17).
#: Схемой meta владеет `save_meta`, поэтому ключ живёт здесь, а не у вызывающего.
PRE_DEDUP_META_KEY = 'pre_dedup_by_source'


def now_iso() -> str:
    """Текущий момент в ISO-UTC — метка фактической дозагрузки описания (_enriched_at)."""
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def load_raw() -> list[dict[str, Any]]:
    with open(RAW_FILE, encoding='utf-8') as f:
        items: list[dict[str, Any]] = json.load(f)
    return items


def save_raw(items: list[dict[str, Any]]) -> None:
    # Атомарно (tmp + os.replace): крэш посреди записи не портит единственный файл
    # многочасового сбора.
    atomic_write_json(RAW_FILE, items)
    save_meta(len(items))


def load_desc_cache() -> dict[str, dict[str, Any]]:
    """Кеш описаний из прошлого сбора: {id: запись}.

    Схема записи — {sig, description_html, requirement, at, experience_id}:

    * `sig` — маркер изменения (hirify: updated_at; hh: published_at);
    * `description_html` — полное описание;
    * `requirement` — текст сниппета/карточки; нужен HH: enrich заполняет и текст карточки
      (питает _detect_techs/_detect_role);
    * `at` — когда реально дозагружено (для max-age);
    * `experience_id` — код доменного VO опыта (`Experience.hh_id`) из уже сохранённой
      raw-схемы (`repository.py::_to_dict` пишет `experience.id`).

    ПОСЛЕДНИЙ КЛЮЧ — НЕОБЯЗАТЕЛЬНЫЙ, он для двухфазных источников, у которых грейд приходит
    ТОЛЬКО в карточке (getmatch: `seniority` + `required_years_of_experience`). Без него
    переиспользование описания из кеша молча теряло опыт: описание бралось из кеша, карточка
    повторно не тянулась, а грейда в списке нет вовсе — ~740 записей getmatch шли без опыта
    13 дней из 14 (аудит 08.08.2026). Значение может быть `None` (опыт не указан или запись
    старого формата) — читатель обязан это выдержать; поднимает его обратно в VO
    `getmatch.py::_cached_experience` через `Experience.from_code`.

    СЫРЫХ полей грейда (`seniority`/`years`) здесь НЕТ намеренно: `experience.id` персистится
    с самого начала, и round-trip `Experience.hh_id -> from_code` возвращает тот же VO, так
    что второй путь ничего не восстановил бы, а в схеме хранения появились бы два примитива-
    дубля доменного `Vacancy.experience`. Раньше ключи `seniority`/`years` в записи были и
    ВСЕГДА равнялись None (`repository.py::_to_dict` их не пишет) — контракт обещал то,
    чего нет.

    Только реально обогащённые записи (_enriched) — tldr-заглушки не должны блокировать
    будущую дозагрузку."""
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
                       'at': it.get('_enriched_at'),      # когда реально дозагружено (для max-age)
                       # грейд из карточки: у записи без опыта ключ пуст -> None, это штатно
                       'experience_id': (it.get('experience') or {}).get('id') or None}
            for it in prior
            if it.get('_enriched') and it.get('description_html')}


def cache_hit_matches(hit: dict[str, Any] | None, cur_sig: str) -> TypeGuard[dict[str, Any]]:
    """Запись кеша про ТУ ЖЕ версию вакансии и с непустым описанием — БЕЗ учёта возраста.

    Отделено от `cache_hit_usable` намеренно: «протухло» и «не про эту вакансию» — разные
    вещи, и путать их дорого. Протухшее описание можно показать как есть, когда бюджета на
    пере-обогащение не осталось (иначе адаптер откатывается на tldr-заглушку и ТЕРЯЕТ уже
    скачанный текст); описание от другой версии вакансии переиспользовать нельзя вовсе."""
    return bool(hit and hit['sig'] == cur_sig and hit['description_html'])


def cache_hit_usable(hit: dict[str, Any] | None, cur_sig: str) -> TypeGuard[dict[str, Any]]:
    """Годна ли запись кеша: сигнал совпал, описание есть и не старше DESC_CACHE_MAX_AGE_DAYS
    (предохранитель от тихой правки без смены сигнала). 0 -> без ограничения по времени.

    TypeGuard, а не bool: True гарантирует `hit is not None`, и вызывающий (hh.py::run_enrich)
    читает поля записи без повторной проверки на None."""
    if not cache_hit_matches(hit, cur_sig):
        return False
    at = (hit or {}).get('at')
    if DESC_CACHE_MAX_AGE_DAYS and at:
        age = freshness.age_days(at)
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


def load_pre_dedup_counts() -> dict[str, int]:
    """Объёмы источников ДО кросс-портального дедупа из meta; ключа нет / формат не тот -> {}.

    Мягкий контракт: файл на диске мог остаться от версии без ключа или быть поправлен руками,
    поэтому любое отклонение — не падение, а пустой результат. Вызывающий
    (`hh.py::_prior_source_counts`) на пусто берёт свой фолбэк: post-dedup счётчики кеша.
    Порог там мягче заявленного, зато ложной блокировки записи кеша нет."""
    meta: dict[str, Any] = read_json_or(META_FILE, {})
    raw: Any = meta.get(PRE_DEDUP_META_KEY)
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for src, n in raw.items():
        if not isinstance(src, str) or isinstance(n, bool) or not isinstance(n, int) or n < 0:
            log.warning('meta.{}: неожиданная запись {!r}: {!r} — pre-dedup объёмы игнорирую '
                        'целиком, гейт сравнит с post-dedup кешем.', PRE_DEDUP_META_KEY, src, n)
            return {}
        counts[src] = n
    return counts


def save_meta(count: int, pre_dedup_by_source: Mapping[str, int] | None = None) -> None:
    """Метаданные последнего сбора. Файл переписывается ЦЕЛИКОМ, поэтому
    `pre_dedup_by_source` — не «добавка», а владение ключом `PRE_DEDUP_META_KEY`.

    `None` означает «состав источников не менялся» (так зовёт `hh.py::enrich`): прошлая база
    санити-гейта переносится из существующего файла. Без переноса дозагрузка карточек молча
    стирала бы базу, и следующий сбор съезжал на мягкий post-dedup фолбэк. Старая meta без
    ключа — норма, мигрировать нечего: база запишется по итогам первого же сбора."""
    counts = load_pre_dedup_counts() if pre_dedup_by_source is None else dict(pre_dedup_by_source)
    meta: dict[str, Any] = {
        'collected_at': time.time(),
        'cities': list(CITIES.keys()),
        'queries': SEARCH_QUERIES,
        'sources': SOURCES,
        'count': count,
    }
    if counts:                                   # пусто == базы нет; ключ-пустышку не пишем
        meta[PRE_DEDUP_META_KEY] = counts
    atomic_write_json(META_FILE, meta, indent=2)
