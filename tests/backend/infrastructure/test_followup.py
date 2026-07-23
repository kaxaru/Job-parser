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
    assert len(data) == 1 and data["1"]["status"] == "empty"


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
    assert first["id"] == "1" and first["cover"] == "c1"            # FIFO + поля сохранены
    assert [x["id"] for x in followup.load_pending()] == ["2"]
    assert followup.pop_pending_one()["id"] == "2"
    assert followup.pop_pending_one() is None
