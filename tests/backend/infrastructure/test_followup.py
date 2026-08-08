"""Тесты форм-очереди (вакансии-опросники): идемпотентная запись в JSON."""
import pytest

from hrwork.infrastructure.storage import followup


@pytest.fixture
def tmp_forms(tmp_path, monkeypatch):
    f = tmp_path / "form_vacancies.json"
    monkeypatch.setattr(followup, "FORM_VACANCIES_FILE", f)
    monkeypatch.setattr(followup, "DATA_DIR", tmp_path)
    return f


def test_load_missing_empty(tmp_forms):
    assert followup.load_form_vacancies() == {}


def test_add_and_load(tmp_forms):
    followup.add_form_vacancy("132664467", "Ведущий Python", "https://hh.ru/vacancy/132664467")
    data = followup.load_form_vacancies()
    assert data["132664467"]["name"] == "Ведущий Python"
    assert data["132664467"]["url"].endswith("132664467")


def test_add_is_idempotent(tmp_forms):
    followup.add_form_vacancy("1", "A", "u1")
    followup.add_form_vacancy("1", "A-изменено", "u1")   # тот же id -> не перезаписываем
    data = followup.load_form_vacancies()
    assert len(data) == 1
    assert data["1"]["name"] == "A"


def test_add_multiple(tmp_forms):
    followup.add_form_vacancy("1", "A", "u1")
    followup.add_form_vacancy("2", "B", "u2")
    assert set(followup.load_form_vacancies()) == {"1", "2"}


def test_add_reports_new_vacancy_and_duplicate_apart(tmp_forms):
    # Возврат нужен счётчику прогона: анкета — не отклик и не пропуск, и пока она падала
    # в очередь молча, массовое включение опросников на HH было неотличимо от нормы
    # (аудит 08.08.2026, «FORM-исходы невидимы в сводке прогона»).
    assert followup.add_form_vacancy("1", "A", "u1") is True
    assert followup.add_form_vacancy("1", "A", "u1") is False


# ── Кеш структуры анкет: свип пишет сюда, чтобы не гонять форму повторно ──
@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(followup, "FORM_CACHE_FILE", tmp_path / "forms_cache.json")
    monkeypatch.setattr(followup, "DATA_DIR", tmp_path)


def test_form_cache_empty_by_default(tmp_cache):
    assert followup.load_form_cache() == {}
    assert followup.cached_form_ids() == set()


def test_cache_form_roundtrip(tmp_cache):
    followup.cache_form("111", "Job", "https://hh.ru/vacancy/111",
                        [{"prompt": "Q?", "ftype": "radio", "options": ["Да", "Нет"]}], "ok")
    c = followup.load_form_cache()
    assert c["111"]["status"] == "ok"
    assert c["111"]["fields"][0]["options"] == ["Да", "Нет"]
    assert c["111"]["ts"]                                    # проставлен автоматически
    assert "111" in followup.cached_form_ids()


def test_cache_form_overwrites_same_id(tmp_cache):
    followup.cache_form("1", "A", "u", [{"prompt": "x"}], "ok")
    followup.cache_form("1", "A", "u", [], "empty")          # тот же id -> обновляем (не дублируем)
    data = followup.load_form_cache()
    assert list(data) == ["1"]
    assert data["1"]["status"] == "empty"


# ── Очередь ожидания (лента -> крон): FIFO, идемпотентность по id ──
@pytest.fixture
def tmp_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(followup, "PENDING_FILE", tmp_path / "apply_pending.json")
    monkeypatch.setattr(followup, "DATA_DIR", tmp_path)


def test_pending_empty_by_default(tmp_pending):
    assert followup.load_pending() == []
    assert followup.pop_pending_one() is None


def test_pending_enqueue_fifo_and_idempotent(tmp_pending):
    assert followup.enqueue_pending("1", "u1", "n1", "c1") == 1
    assert followup.enqueue_pending("2", "u2", "n2", "") == 2
    assert followup.enqueue_pending("1", "u1", "n1", "c1") == 2      # дубль по id — не растёт
    first = followup.pop_pending_one()
    assert (first["id"], first["cover"]) == ("1", "c1")             # FIFO + поля сохранены
    assert [x["id"] for x in followup.load_pending()] == ["2"]
    assert followup.pop_pending_one()["id"] == "2"
    assert followup.pop_pending_one() is None


def test_pending_keeps_the_employer_known_at_click_time(tmp_pending):
    # работодателя знает ЛЕНТА в момент клика; к дренажу вакансия может уйти из выдачи,
    # а в журнале без него карточка-призрак не ищется по компании (инцидент 01.08.2026)
    followup.enqueue_pending("1", "u1", "n1", "c1", employer="ООО Ромашка")
    assert followup.pop_pending_one()["employer"] == "ООО Ромашка"


def test_pending_employer_defaults_to_empty(tmp_pending):
    followup.enqueue_pending("1", "u1", "n1", "c1")
    assert followup.pop_pending_one()["employer"] == ""


# ── Возврат в очередь: снятая, но НЕ обработанная запись не имеет права пропасть ──
# АУДИТ 08.08.2026: pop_pending_one удаляет запись с диска ДО обработки, а дренаж глотал
# исключение и рапортовал «+1 доп. отклик». Систематическая ошибка выела бы всю очередь молча.

def test_requeue_returns_the_record_whole(tmp_pending):
    followup.enqueue_pending("1", "u1", "n1", "c1")
    rec = followup.pop_pending_one()
    assert followup.requeue_pending(rec) == 1
    assert followup.load_pending() == [{"id": "1", "url": "u1", "name": "n1", "cover": "c1",
                                        "employer": ""}]


def test_requeue_keeps_fields_the_queue_does_not_know_about(tmp_pending):
    # возврат кладёт запись ЦЕЛИКОМ, а не пересобирает её из четырёх известных полей
    followup.requeue_pending({"id": "7", "url": "u7", "name": "n7", "cover": "c7",
                              "employer": "ООО Ромашка"})
    assert followup.load_pending()[0]["employer"] == "ООО Ромашка"


def test_requeue_puts_the_record_at_the_tail(tmp_pending):
    # в конец, а не в начало: сбойная запись не должна блокировать остальную очередь
    followup.enqueue_pending("1", "u1", "n1", "c1")
    followup.enqueue_pending("2", "u2", "n2", "c2")
    rec = followup.pop_pending_one()
    followup.requeue_pending(rec)
    assert [x["id"] for x in followup.load_pending()] == ["2", "1"]


def test_requeue_is_idempotent_by_id(tmp_pending):
    followup.requeue_pending({"id": "1", "url": "u1", "name": "n1", "cover": "c1"})
    assert followup.requeue_pending({"id": "1", "url": "u1", "name": "n1", "cover": "c1"}) == 1
    assert len(followup.load_pending()) == 1
