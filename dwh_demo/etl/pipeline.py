"""Конвейер ETL: extract+transform делаются ОДИН раз (в Python), затем результат
грузится в каждое инжектированное хранилище. Бэкенд — внешняя зависимость (DI).

Загрузка ПОЛНАЯ: каждый адаптер сначала чистит факт (TRUNCATE/DELETE), потом заливает
срез целиком. Поэтому деградированный вход здесь так же опасен, как деградированный
сбор у родителя (`hh.py::collect`): затерев факт нулём, вчерашние данные не вернуть.
Отсюда два санити-гейта (`Pipeline._check_broken_share`, `Pipeline._reject_degraded`)
и обязательная сверка результата (`Pipeline._verify`).
"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import Settings
from .domain import Vacancy
from .rates import load_rates
from .source import JsonSource
from .warehouse import ClickHouseWarehouse, MSSQLWarehouse, PostgresWarehouse, Warehouse

SQL_DIR = Path(__file__).resolve().parent / "sql"

# реестр доступных бэкендов: имя -> как собрать адаптер из настроек
REGISTRY = {
    "postgres": lambda cfg: PostgresWarehouse(cfg.pg_dsn, SQL_DIR / "postgres" / "schema.sql"),
    "clickhouse": lambda cfg: ClickHouseWarehouse(cfg.ch_url, SQL_DIR / "clickhouse" / "schema.sql"),
    "mssql": lambda cfg: MSSQLWarehouse(cfg.mssql_dsn, SQL_DIR / "mssql" / "schema.sql"),
}
TARGETS = tuple(REGISTRY)

# ── Пороги санити-гейтов ──
# Те же числа и тот же смысл, что у родителя (`hrwork/config.py::COLLECT_MIN_RATIO`,
# `COLLECT_SANITY_MIN`): срез, просевший больше чем вдвое относительно того, что УЖЕ лежит
# в факте, — признак сбоя источника, а не новости рынка. Такой срез не грузим.
# Обход — осознанный, флагом: `python -m etl load --force` (аналог `hh.py collect --force`).
LOAD_MIN_RATIO = 0.5
# Ниже этого объёма доли не показательны (демо-фикстура из 5 записей), и гейты молчат.
SANITY_MIN_ROWS = 500
# Доля записей, которые не разобрал домен. Кривая запись из внешнего JSON — норма
# (пропускаем), но 1 % и выше означает смену формата поля у родителя, то есть НАШУ
# поломку -> падаем, а не грузим огрызок.
MAX_BROKEN_RATIO = 0.01
# Пороги живут константами модуля, а не в Settings: это политика конвейера, одинаковая
# на всех стендах, тогда как Settings — параметры подключения конкретного окружения.


def log(msg: str) -> None:
    print(f"[etl] {msg}", flush=True)


def log_error(msg: str) -> None:
    """Аварийная строка прогона: маркер `ERROR` + stderr.

    Обычный `log` пишет в stdout без уровня, и строка `[etl] [postgres] loaded: 0`
    в Loki неотличима от успеха. По подстроке `[etl] ERROR` строится алерт, и она же
    попадает в лог таски Airflow отдельным потоком."""
    print(f"[etl] ERROR {msg}", file=sys.stderr, flush=True)


class Pipeline:
    def __init__(self, source: JsonSource, warehouses: list[Warehouse],
                 fx_file: Path | None = None):
        self.source = source
        self.warehouses = warehouses
        # Путь к кешу курсов — необязательный: None означает дефолт `config.FX_DEFAULT`.
        # Инжектируется, а не читается из окружения внутри, чтобы тест мог подсунуть свой
        # файл, не трогая переменные процесса.
        self.fx_file = fx_file
        self._records: list[Vacancy] | None = None

    def prepare(self) -> list[Vacancy]:
        """extract + transform (один раз, кэшируется).

        Отбраковка считается ПО ПРИЧИНАМ: три молчаливых `continue` с одним итогом
        «стало меньше» не дают отличить сломавшийся разбор от выросшей доли дублей.
        """
        if self._records is None:
            recs: list[Vacancy] = []
            no_id = broken = dup = 0
            # Курсы читаются ОДИН раз и передаются явно, а не добираются доменом по
            # ленивому дефолту: ввод-вывод на границе конвейера видно, а внутри разбора —
            # нет. Пустой словарь (нет кеша) прогон не роняет: рублёвые поля станут NULL
            # и вакансия выпадет из зарплатного среза, а не приедет в него нулём.
            fx = load_rates(self.fx_file)
            # Дедуп по id — защита КОНВЕЙЕРА, а не следствие поискового запроса: поля
            # `_query` в сырых данных больше нет (09.08.2026), но кеш родителя по-прежнему
            # копится несколькими прогонами по восьми порталам, и повтор id уронил бы
            # PG/MSSQL по PRIMARY KEY, а в ClickHouse (MergeTree, ключ не уникален) молча
            # задвоил бы счётчики витрин.
            seen: set[str] = set()
            for r in self.source.read():
                if not isinstance(r, dict):
                    broken += 1        # чужие данные: элемент не объект -> деградируем
                    continue
                if not r.get("id"):
                    no_id += 1
                    continue
                try:
                    v = Vacancy.from_raw(r, fx=fx)
                except (AttributeError, KeyError, ValueError, TypeError):
                    # AttributeError — тот же класс «в чужом JSON не тот тип» (`salary`
                    # строкой вместо объекта): раньше он ронял ВЕСЬ прогон на одной записи.
                    # Проглатывание своей ошибки ловит гейт `_check_broken_share` ниже.
                    broken += 1
                    continue
                if v.id in seen:
                    dup += 1
                    continue
                seen.add(v.id)
                recs.append(v)
            with_rub = sum(1 for v in recs if v.salary_min_rub is not None
                           or v.salary_max_rub is not None)
            log(f"prepare: {len(recs)} вакансий (extract+transform); "
                f"пропущено: no_id={no_id} broken={broken} dup={dup}; "
                f"FX: {len(fx)} курсов, вилка в рублях у {with_rub}")
            self._check_broken_share(len(recs) + no_id + broken + dup, broken)
            self._records = recs
        return self._records

    @staticmethod
    def _check_broken_share(total: int, broken: int) -> None:
        """Fail fast на СВОЕЙ ошибке: `Vacancy.from_raw` — наш код, и массовое исключение
        в нём означает смену формата поля у родителя. Раньше такой дрейф давал ноль
        записей и зелёный прогон, потому что `except` стоит вокруг каждой записи."""
        if total >= SANITY_MIN_ROWS and broken > total * MAX_BROKEN_RATIO:
            log_error(f"не разобрано {broken} записей из {total} (порог {MAX_BROKEN_RATIO:.0%}) — "
                      f"вероятна смена формата данных, загрузка отменена")
            raise RuntimeError(f"prepare: битых записей {broken} из {total}")

    def init_schema(self) -> None:
        for wh in self.warehouses:
            wh.init_schema()
            log(f"[{wh.name}] schema ready")

    def load(self, force: bool = False) -> None:
        """Полный перезалив факта во все инжектированные хранилища.

        Политика восстановления. Каждое хранилище по отдельности перезаливается целиком
        или не перезаливается вовсе (см. политику в адаптерах). Fan-out целиком НЕ атомарен:
        падение на втором адаптере оставит первый с новым срезом, третий — со вчерашним.
        Повтор безопасен и обязателен: загрузка идемпотентна (полная замена факта).
        О неполном результате узнаёт оператор — строкой `[etl] ERROR` в stderr и падением
        прогона (в Airflow — красной таской), а не расхождением цифр на дашборде.
        """
        records = self.prepare()
        if not records:
            # Пусто — это НЕ деградация источника, а отсутствие входа: молча обнулять
            # три хранилища нельзя. Тот же класс, что `FileNotFoundError` в JsonSource.
            log_error("prepare не дал ни одной вакансии — хранилища не тронуты")
            raise RuntimeError("нечего грузить: prepare вернул 0 вакансий")
        if not force:
            self._reject_degraded(records)
        counts: dict[str, int] = {}
        for wh in self.warehouses:
            try:
                counts[wh.name] = wh.load(records)
            except Exception:
                log_error(f"[{wh.name}] загрузка упала; уже перезалиты: {list(counts)} — "
                          f"движки рассинхронизированы, повторить прогон целиком")
                raise
            log(f"[{wh.name}] loaded: {counts[wh.name]}")
        self._verify(counts, len(records))

    def _reject_degraded(self, records: list[Vacancy]) -> None:
        """Санити-гейт полного перезалива: сравнить срез с тем, что уже в факте.

        Проверяются ВСЕ хранилища до загрузки первого: иначе первый адаптер успел бы
        затереть свой факт раньше, чем гейт скажет «нет»."""
        degraded = []
        for wh in self.warehouses:
            try:
                prior = wh.count()
            except Exception as e:
                # Хранилище может быть ещё без схемы (`load` без `init`) — гейт не должен
                # быть строже самой загрузки: она на том же месте упадёт сама и внятно.
                log(f"[{wh.name}] прошлый объём недоступен ({e}) — санити-гейт пропущен")
                continue
            if prior >= SANITY_MIN_ROWS and len(records) < prior * LOAD_MIN_RATIO:
                degraded.append(f"{wh.name}: {len(records)} << {prior}")
        if degraded:
            log_error(f"вход просел ниже {LOAD_MIN_RATIO:.0%} от факта ({'; '.join(degraded)}) — "
                      f"перезалив отменён, данные сохранены. Повтор при реальном спаде: --force")
            raise RuntimeError(f"деградированный вход: {'; '.join(degraded)}")

    @staticmethod
    def _verify(counts: dict[str, int], expected: int) -> None:
        """Главная проверка проекта — числом, а не глазом по трём строкам stdout.

        `Warehouse.load` обязан вернуть число строк В ФАКТЕ, поэтому у исправных адаптеров
        оно равно результату `prepare` у всех движков сразу. Расхождение = адаптеры
        разошлись либо часть строк не доехала."""
        log(f"verify: prepare={expected}, в факте {counts}")
        bad = {name: n for name, n in counts.items() if n != expected}
        if bad:
            log_error(f"в факте не то, что подготовлено (prepare={expected}): {bad} — "
                      f"адаптеры разошлись")
            raise RuntimeError(f"расхождение факта с prepare={expected}: {bad}")

    def run(self, steps, force: bool = False) -> None:
        if not steps or "all" in steps:
            steps = ["init", "load"]
        if "init" in steps:
            self.init_schema()
        if "load" in steps:
            self.load(force=force)


def make_warehouse(name: str, cfg: Settings) -> Warehouse:
    return REGISTRY[name](cfg)


def build_pipeline(targets=TARGETS, cfg: Settings | None = None) -> Pipeline:
    """Фабрика: собирает конвейер с источником и адаптерами выбранных бэкендов."""
    cfg = cfg or Settings.from_env()
    warehouses = [make_warehouse(t, cfg) for t in targets]
    return Pipeline(JsonSource(cfg.data_file), warehouses, fx_file=cfg.fx_file)
