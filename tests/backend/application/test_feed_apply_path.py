"""Путь отклика из ленты: что попадает в журнал и что происходит с очередью ожидания.

Две находки аудита 08.08.2026, обе про потерю данных на НЕОБРАТИМОМ пути.

1. ЖУРНАЛ (`autoclick.py::_apply_one_vacancy`). В `applied_log.jsonl` уходили сырой аргумент
   `name` и всегда пустой `employer`: `Candidate` создавался без работодателя. Вакансия уходит
   из выдачи, карточка-призрак в ленте синтезируется из журнала — и без работодателя она не
   находится ни поиском по компании, ни воронкой `funnel.py`. Это вторая половина инцидента
   01.08.2026, закрытая тогда только для forms-пути.

2. ОЧЕРЕДЬ (`autoclick.py::_drain_pending`). `pop_pending` снимает запись с диска ДО обработки,
   вызов был обёрнут в `contextlib.suppress(Exception)`, результат игнорировался, а счётчик рос
   безусловно. Клик в ленте при занятом кроне -> запись в очередь -> НАША ошибка при дренаже ->
   исключение проглочено, запись уже удалена: отклика нет, лога нет, в сводке «+1 доп. отклик».
"""
import pytest

from hrwork.application.apply import autoclick
from hrwork.application.apply.outcome import ApplyOutcome
from hrwork.infrastructure.storage import followup

VACANCY_URL = "https://hh.ru/vacancy/777"


@pytest.fixture
def journal(monkeypatch):
    """Отклик всегда «уходит»; записываем только то, что попало в журнал."""
    rows: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(autoclick, "apply_one", lambda *a, **k: ApplyOutcome.APPLIED)
    monkeypatch.setattr(autoclick.store, "mark_applied", lambda vid: None)
    monkeypatch.setattr(autoclick.store, "bump_quota", lambda n: n)
    monkeypatch.setattr(autoclick.store, "log_applied",
                        lambda vid, name, url, **kw: rows.append((vid, name, url,
                                                                  kw.get("employer", ""))))
    monkeypatch.setattr(autoclick, "_send_cover_via_chat", lambda *a, **k: True)
    monkeypatch.setattr(autoclick, "_VACANCY_META", {})
    return rows


def test_feed_apply_journals_employer_from_the_vacancy_cache(journal, monkeypatch):
    monkeypatch.setattr(autoclick, "_VACANCY_META",
                        {"777": ("Python-разработчик", "ООО Ромашка")})
    autoclick._apply_one_vacancy(None, "777", VACANCY_URL, "", name="Python-разработчик")
    assert journal == [("777", "Python-разработчик", VACANCY_URL, "ООО Ромашка")]


def test_feed_apply_journals_explicit_employer(journal):
    autoclick._apply_one_vacancy(None, "777", VACANCY_URL, "", name="Python-разработчик",
                                 employer="Сбер. IT")
    assert journal == [("777", "Python-разработчик", VACANCY_URL, "Сбер. IT")]


def test_feed_apply_never_journals_a_nameless_vacancy(journal):
    # журнал append-only: пустое имя задним числом не чинится, вакансия навсегда осталась бы
    # безымянной в «моих откликах» и в воронке. Фолбэк — id вакансии.
    autoclick._apply_one_vacancy(None, "777", VACANCY_URL, "", name="")
    assert journal == [("777", "777", VACANCY_URL, "")]


def test_feed_apply_prefers_the_cached_name_over_the_id_fallback(journal, monkeypatch):
    monkeypatch.setattr(autoclick, "_VACANCY_META", {"777": ("Data Engineer", "Ozon")})
    autoclick._apply_one_vacancy(None, "777", VACANCY_URL, "", name="")
    assert journal == [("777", "Data Engineer", VACANCY_URL, "Ozon")]


# ── Дренаж очереди ожидания ────────────────────────────────────────────────────────────
@pytest.fixture
def queue(tmp_path, monkeypatch):
    """Настоящая очередь на диске во временном каталоге + отключённые паузы."""
    monkeypatch.setattr(followup, "PENDING_FILE", tmp_path / "apply_pending.json")
    monkeypatch.setattr(followup, "DATA_DIR", tmp_path)
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 0)
    monkeypatch.setattr(autoclick.time, "sleep", lambda s: None)
    return followup


def _outcomes(monkeypatch, by_id: dict[str, str]) -> list[str]:
    """Заглушка отклика: исход задаётся по id вакансии. Возвращает список попыток."""
    tried: list[str] = []

    def one(page, vid, url, cover_text, name="", employer=""):
        tried.append(str(vid))
        return {"status": by_id[str(vid)], "letter": False}

    monkeypatch.setattr(autoclick, "_apply_one_vacancy", one)
    return tried


def test_only_applied_outcomes_are_counted(queue, monkeypatch):
    queue.enqueue_pending("1", "u1", "n1", "c1")
    queue.enqueue_pending("2", "u2", "n2", "c2")
    queue.enqueue_pending("3", "u3", "n3", "c3")
    _outcomes(monkeypatch, {"1": "applied", "2": "skip", "3": "form"})
    assert autoclick._drain_pending(page=None, daily_cap=200) == 1
    assert queue.load_pending() == []          # исход дошёл до всех трёх — очередь пуста


def test_failed_item_returns_to_the_queue_whole(queue, monkeypatch):
    queue.enqueue_pending("1", "u1", "Python-разработчик", "моё письмо")

    def boom(*a, **k):
        raise TypeError("build_cover() got an unexpected keyword argument")

    monkeypatch.setattr(autoclick, "_apply_one_vacancy", boom)
    assert autoclick._drain_pending(page=None, daily_cap=200) == 0
    assert queue.load_pending() == [{"id": "1", "url": "u1", "name": "Python-разработчик",
                                     "cover": "моё письмо", "employer": ""}]


def test_systematic_failure_does_not_eat_the_queue(queue, monkeypatch):
    for i in ("1", "2", "3"):
        queue.enqueue_pending(i, f"u{i}", f"n{i}", f"c{i}")
    tried: list[str] = []

    def boom(page, vid, *a, **k):
        tried.append(str(vid))
        raise RuntimeError("смена вёрстки HH")

    monkeypatch.setattr(autoclick, "_apply_one_vacancy", boom)
    assert autoclick._drain_pending(page=None, daily_cap=200) == 0
    assert tried == ["1", "2", "3"]                                   # по одной попытке на запись
    assert sorted(x["id"] for x in queue.load_pending()) == ["1", "2", "3"]


def test_returned_item_is_not_retried_twice_in_one_run(queue, monkeypatch):
    # возврат идёт в КОНЕЦ очереди, поэтому без защиты дренаж крутил бы её бесконечно
    queue.enqueue_pending("1", "u1", "n1", "c1")
    tried: list[str] = []

    def boom(page, vid, *a, **k):
        tried.append(str(vid))
        raise RuntimeError("наша ошибка")

    monkeypatch.setattr(autoclick, "_apply_one_vacancy", boom)
    autoclick._drain_pending(page=None, daily_cap=200)
    assert tried == ["1"]


def test_our_error_in_cover_returns_the_item(queue, monkeypatch):
    # письмо строится ДО отклика; наша ошибка здесь тоже не имеет права съесть запись
    queue.enqueue_pending("1", "u1", "n1", "")     # cover пуст -> зовётся build_cover

    def boom(cand, mode="template"):
        raise AttributeError("'Candidate' object has no attribute 'desc'")

    monkeypatch.setattr(autoclick.cover, "build_cover", boom)
    assert autoclick._drain_pending(page=None, daily_cap=200) == 0
    assert [x["id"] for x in queue.load_pending()] == ["1"]


def test_captcha_stops_the_drain_and_keeps_the_rest_queued(queue, monkeypatch):
    # под капчей каждый следующий клик — ещё один бот-сигнал HH (та же логика, что в _apply_batch)
    for i in ("1", "2", "3"):
        queue.enqueue_pending(i, f"u{i}", f"n{i}", f"c{i}")
    tried = _outcomes(monkeypatch, {"1": "captcha", "2": "applied", "3": "applied"})
    assert autoclick._drain_pending(page=None, daily_cap=200) == 0
    assert tried == ["1"]
    assert sorted(x["id"] for x in queue.load_pending()) == ["1", "2", "3"]


def test_drain_stops_at_the_daily_cap(queue, monkeypatch):
    queue.enqueue_pending("1", "u1", "n1", "c1")
    monkeypatch.setattr(autoclick.store, "applied_today", lambda: 200)
    tried = _outcomes(monkeypatch, {"1": "applied"})
    assert autoclick._drain_pending(page=None, daily_cap=200) == 0
    assert tried == []
    assert [x["id"] for x in queue.load_pending()] == ["1"]      # очередь не тронута


def test_drain_passes_employer_from_the_queue_record(queue, monkeypatch):
    queue.requeue_pending({"id": "1", "url": "u1", "name": "n1", "cover": "c1",
                           "employer": "ООО Ромашка"})
    got: list[str] = []

    def one(page, vid, url, cover_text, name="", employer=""):
        got.append(employer)
        return {"status": "applied", "letter": False}

    monkeypatch.setattr(autoclick, "_apply_one_vacancy", one)
    autoclick._drain_pending(page=None, daily_cap=200)
    assert got == ["ООО Ромашка"]
