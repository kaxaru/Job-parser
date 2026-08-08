"""Агрегация статистики по вакансиям (ViewModel-слой)."""
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from hrwork.config import LANG_KEYS, MIN_SAMPLE_CITY_LANG, MIN_SAMPLE_SALARY
from hrwork.domain import freshness
from hrwork.domain.models import Vacancy
from hrwork.infrastructure.net import rates

# «Удалённо» для срезов спрашиваем У ДОМЕНА (`Vacancy.is_remote_like`). Свой кортеж
# `_REMOTE = (REMOTE, HYBRID)` здесь был вторым ответом на тот же вопрос и разошёлся с
# фильтром ленты: 16 857 вакансий одновременно «офис» в ленте и «удалёнка» в отчётах
# 09/11/12 и графиках chart_remote/chart_by_source (инцидент 08.08.2026). Предикат
# ровно один; смысл среза при этом прежний — гибрид считается удалёнкой.

# Округление агрегатов — `round`, а не `int`: усечение давало «4166 там, где верно 4167»
# (тот же разбор, что в `salary.py::net`). На .5 работает банковское округление Python —
# симметрия с доменом важнее, чем половина рубля или полдня возраста.


class Analyzer:

    def __init__(self, vacancies: list[Vacancy], fx: dict[str, Any]) -> None:
        """Зарплатные срезы нормализуем в RUB по FX-курсам (агрегатор: HH=RUR, hirify=USD/EUR/…;
        раньше брали только RUR -> hirify выпадал). fx инжектится ЯВНО View-слоем (reporter) —
        конструктор в сеть НЕ ходит (тестируемость, нет скрытого I/O). Для standalone/CLI —
        фабрика Analyzer.with_live_rates()."""
        self.vacs = vacancies
        # (Vacancy, зарплата в RUB) — НЕ мутируем сущность полем salary_rub (раньше Analyzer
        # писал в общий инстанс -> aliasing при повторном анализе того же списка с другим fx).
        self.paid: list[tuple[Vacancy, int]] = []
        for v in vacancies:
            rub = rates.to_rub(v.salary.mid, v.salary.currency, fx) if v.salary else None
            if rub is None:
                continue
            self.paid.append((v, rub))

    @classmethod
    def with_live_rates(cls, vacancies: list[Vacancy]) -> "Analyzer":
        """Фабрика для standalone/CLI: тянет актуальные FX-курсы и конструирует Analyzer.
        Сеть только здесь, явно — конструктор остаётся чистым."""
        return cls(vacancies, fx=rates.get_rates())

    def count_by_city(self) -> Counter[str]:
        return Counter(v.city for v in self.vacs)

    def tech_by_city(self) -> dict[str, Counter[str]]:
        res: dict[str, Counter[str]] = defaultdict(Counter)
        for v in self.vacs:
            for tech in v.techs:
                res[v.city][tech] += 1
        return dict(res)

    def salary_by_lang(self) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[int]] = defaultdict(list)
        for v, rub in self.paid:
            for tech in v.techs:
                if tech in LANG_KEYS:
                    buckets[tech].append(rub)
        out = {}
        for lang, sals in buckets.items():
            if len(sals) < MIN_SAMPLE_SALARY:
                continue
            qs = statistics.quantiles(sals, n=4)
            out[lang] = {
                'n':      len(sals),
                'median': round(statistics.median(sals)),
                'mean':   round(statistics.mean(sals)),
                'p25':    round(qs[0]),
                'p75':    round(qs[2]),
                'min':    min(sals),
                'max':    max(sals),
            }
        return out

    def salary_city_lang(self) -> dict[str, dict[str, dict[str, Any]]]:
        buckets: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for v, rub in self.paid:
            for tech in v.techs:
                if tech in LANG_KEYS:
                    buckets[v.city][tech].append(rub)
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for city, langs in buckets.items():
            out[city] = {}
            for lang, sals in langs.items():
                if len(sals) < MIN_SAMPLE_CITY_LANG:
                    continue
                qs = statistics.quantiles(sals, n=4) if len(sals) >= 4 else None
                out[city][lang] = {
                    'n':      len(sals),
                    'median': round(statistics.median(sals)),
                    'p25':    round(qs[0]) if qs else None,
                    'p75':    round(qs[2]) if qs else None,
                }
        return out

    def top_stacks(self, n: int = 20) -> list[tuple[Any, int]]:
        pairs: Counter[Any] = Counter()
        for v in self.vacs:
            langs = [t for t in v.techs if t in LANG_KEYS]
            for i in range(len(langs)):
                for j in range(i + 1, len(langs)):
                    pairs[tuple(sorted([langs[i], langs[j]]))] += 1
        return pairs.most_common(n)

    def tech_stack_for_lang(self, lang: str) -> list[tuple[str, int]]:
        """Топ сопутствующих технологий в вакансиях, где упомянут `lang`."""
        counter: Counter[str] = Counter()
        for v in self.vacs:
            if lang in v.techs:
                for tech in v.techs:
                    if tech != lang:
                        counter[tech] += 1
        return counter.most_common(25)

    def remote_by_city(self) -> list[dict[str, Any]]:
        city_total: dict[str, int] = {}
        city_remote: dict[str, int] = {}
        for v in self.vacs:
            city_total[v.city] = city_total.get(v.city, 0) + 1
            if v.is_remote_like():
                city_remote[v.city] = city_remote.get(v.city, 0) + 1
        rows = []
        for city, total in city_total.items():
            remote = city_remote.get(city, 0)
            rows.append({
                'city':    city,
                'total':   total,
                'remote':  remote,
                'onsite':  total - remote,
                'pct':     round(remote * 100 / total, 1),
            })
        return sorted(rows, key=lambda x: -x['remote'])

    def freshness_summary(self) -> dict[str, Any]:
        """Распределение вакансий по свежести (fresh/recent/ghost/unknown) + доля гостов.
        Свежесть — по creationTime (реальный возраст), гост = висит >60 дней."""
        FC = freshness.FreshnessClass
        cls = Counter(v.fresh_class() for v in self.vacs)
        dated = sum(cls[k] for k in FC.dated())         # у кого есть дата
        ages = [a for v in self.vacs if (a := v.age_days()) is not None]
        return {
            "total":       len(self.vacs),
            "dated":       dated,
            "counts":      {fc.code: cls.get(fc, 0) for fc in FC},   # code-keyed для CSV/отчётов
            "ghost_pct":   round(cls[FC.GHOST] * 100 / dated, 1) if dated else 0.0,
            "fresh_pct":   round(cls[FC.FRESH] * 100 / dated, 1) if dated else 0.0,
            "median_age":  round(statistics.median(ages)) if ages else None,
        }

    def _group_stats(self, key_field: str,
                     keep: Callable[[Vacancy], bool] | None = None) -> dict[str, dict[str, Any]]:
        """Группировка по атрибуту key_field -> {группа: {total, median_age, ghosts, remote,
        office}}. keep(v) -> False исключает вакансию. Общий костяк by_company/by_source."""
        groups: dict[str, list[Vacancy]] = defaultdict(list)
        for v in self.vacs:
            if keep and not keep(v):
                continue
            groups[getattr(v, key_field)].append(v)
        out: dict[str, dict[str, Any]] = {}
        for g, vacs in groups.items():
            ages = [a for v in vacs if (a := v.age_days()) is not None]
            remote = sum(1 for v in vacs if v.is_remote_like())
            out[g] = {
                "total":      len(vacs),
                "median_age": round(statistics.median(ages)) if ages else None,
                "ghosts":     sum(1 for v in vacs if v.is_ghost()),
                "remote":     remote,
                "office":     len(vacs) - remote,
            }
        return out

    def by_company(self, n: int = 30) -> list[dict[str, Any]]:
        """Топ-N работодателей: сколько вакансий и медиана «сколько висят» (по возрасту
        с creationTime). Много вакансий + высокая медиана возраста = масс-хайринг/гостинг.
        Только IT-роли: ритейл/склад (роль «Не-IT») просачивается из нечёткого поиска HH
        (напр. «товаровед», «разработчик планограмм») и раздувал бы счётчик Fix Price и пр."""
        stats = self._group_stats("employer", keep=lambda v: bool(v.employer) and v.role.is_it)
        rows = [{"company": emp, **s} for emp, s in stats.items()]
        return sorted(rows, key=lambda r: -r["total"])[:n]

    # Корзины размера работодателя. Одиночек тысячи, крупных единицы (степенное
    # распределение), поэтому шаг корзин растёт: 1/2/3/4 поштучно, дальше «5+».
    SIZE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
        ("1", 1, 1), ("2", 2, 2), ("3", 3, 3), ("4", 4, 4),
        ("5–10", 5, 10), ("11–25", 11, 25), ("26–50", 26, 50),
        ("51–100", 51, 100), ("100+", 101, None),
    )

    @staticmethod
    def _size_bucket(total: int) -> str:
        for label, lo, hi in Analyzer.SIZE_BUCKETS:
            if total >= lo and (hi is None or total <= hi):
                return label
        return Analyzer.SIZE_BUCKETS[-1][0]

    def companies_by_size(self) -> list[dict[str, Any]]:
        """Работодатели по числу вакансий -> сколько их и в каком они состоянии.

        Бар = СКОЛЬКО КОМПАНИЙ в корзине, стек = класс свежести компании по МЕДИАНЕ
        возраста её вакансий (у одиночек медиана = возраст единственной вакансии).
        Отвечает на вопрос, который топ-30 не видит: рынок — это длинный хвост
        мелких работодателей, и надо знать, какая его доля протухла."""
        # dict[str, Any]: значения разнородны (str + int + float), а класс свежести
        # набирается по вычисляемому ключу `r[...classify_age(...).code] += 1`.
        rows: list[dict[str, Any]] = []
        for label, _lo, _hi in self.SIZE_BUCKETS:
            rows.append({"bucket": label, "companies": 0, "vacancies": 0,
                         "fresh": 0, "recent": 0, "ghost": 0, "unknown": 0})
        index = {r["bucket"]: r for r in rows}
        for _emp, s in self._group_stats(
                "employer", keep=lambda v: bool(v.employer) and v.role.is_it).items():
            r = index[self._size_bucket(s["total"])]
            r["companies"] += 1
            r["vacancies"] += s["total"]
            r[freshness.classify_age(s["median_age"]).code] += 1
        for r in rows:
            dated = r["companies"] - r["unknown"]
            r["ghost_pct"] = round(r["ghost"] * 100 / dated, 1) if dated else 0.0
        return rows

    def by_source(self) -> list[dict[str, Any]]:
        """Разрез по порталам-источникам (агрегатор): сколько вакансий, медиана возраста,
        удалёнка, гост. Сравнить охват/качество HH vs hirify vs …"""
        rows = [{"source": src, **s} for src, s in self._group_stats("source").items()]
        return sorted(rows, key=lambda r: -r["total"])

    def freshness_by_city(self) -> list[dict[str, Any]]:
        """По городам: свежие / гост / медианный возраст — для дашборда качества выдачи."""
        by_city: dict[str, list[Vacancy]] = defaultdict(list)
        for v in self.vacs:
            by_city[v.city].append(v)
        FC = freshness.FreshnessClass
        rows = []
        for city, vacs in by_city.items():
            cls = Counter(v.fresh_class() for v in vacs)
            dated = sum(cls[k] for k in FC.dated())
            if not dated:
                continue                                    # город без дат — пропускаем
            ages = [a for v in vacs if (a := v.age_days()) is not None]
            rows.append({
                "city":       city,
                "total":      len(vacs),
                "fresh":      cls[FC.FRESH],
                "recent":     cls[FC.RECENT],
                "ghost":      cls[FC.GHOST],
                "ghost_pct":  round(cls[FC.GHOST] * 100 / dated, 1),
                "median_age": round(statistics.median(ages)) if ages else None,
            })
        return sorted(rows, key=lambda r: -r["ghost_pct"])

    def salary_by_experience(self) -> dict[str, dict[str, Any]]:
        buckets: dict[str, list[int]] = defaultdict(list)
        for v, rub in self.paid:
            label = v.experience.label if v.experience else "Не указан"
            buckets[label].append(rub)
        out = {}
        for label, sals in buckets.items():
            if len(sals) < MIN_SAMPLE_SALARY:
                continue
            qs = statistics.quantiles(sals, n=4)
            out[label] = {
                'n':      len(sals),
                'median': round(statistics.median(sals)),
                'p25':    round(qs[0]),
                'p75':    round(qs[2]),
            }
        return out
