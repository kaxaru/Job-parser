"""Стражи моста Python -> JS: офлайн-фолбэк в `src/feed/*.js` == источник в Python.

Зачем отдельный файл. Конвенция (`CLAUDE.md`) разрешает держать в JS ДОСЛОВНУЮ копию
константы только как фолбэк для `file://` без `feed-data.js`. Копия без стража живёт своей
жизнью: к 08.08.2026 `model.js::STATE_LABELS` разошёлся с `chat.py` по двум ключам
(«Звонок» вместо «Телефон-интервью», «Закрыта» вместо «Вакансия закрыта»), а JS-тесты
закрепляли ФОЛБЭК, то есть фиксировали расхождение как требование. Подписи с фолбэка видит
пользователь: лента открывается из `file://` без сервера, и там инжекта нет.

Тесты читают ИСХОДНИК JS, а не собранный бандл: страж обязан работать и без `npm run build`,
и до первого `hh.py feed`.

Чего здесь СОЗНАТЕЛЬНО нет: `resume.js::RESUME_CORE` (фолбэк `['Python','FastAPI']`) —
он сверяется с `config.RESUME_CORE`, который читается из `resume_profile.json` запускающего,
и у форка с другим резюме страж падал бы на чужой настройке, а не на дрейфе кода.
"""
import re
from pathlib import Path

import pytest

from hrwork.application.apply.chat import chat, chat_class
from hrwork.config import PORTAL_SITES
from hrwork.domain.experience import Experience
from hrwork.domain.schedule import REMOTE_LIKE_CODES, Schedule
from hrwork.infrastructure.storage import MARK_VALUES

_SRC = Path(__file__).resolve().parents[3] / "src" / "feed"
_MODEL_JS = (_SRC / "model.js").read_text(encoding="utf-8")
_RESUME_JS = (_SRC / "resume.js").read_text(encoding="utf-8")


def _fallback_src(js: str, injected: str) -> str:
    """Текст литерала-фолбэка, стоящего за `||` после проверки `typeof <injected> !==`.

    Якорь — именно строка кода с `typeof`, а не любое упоминание имени: имена глобалов
    встречаются и в комментариях, и один комментарий перечисляет сразу два (DISCARD/INVITED).
    """
    start = js.index(f"typeof {injected} !==")
    rest = js[js.index("||", start) + 2:].lstrip()
    open_ch = rest[0]
    close_ch = {"{": "}", "[": "]"}[open_ch]
    depth = 0
    for i, ch in enumerate(rest):
        depth += (ch == open_ch) - (ch == close_ch)
        if depth == 0:
            return rest[:i + 1]
    raise AssertionError(f"не найден конец фолбэка для {injected}")


def _js_dict(text: str) -> dict[str, str]:
    """JS-литерал `{ключ: 'значение', …}` -> dict (ключи — идентификаторы, значения строки)."""
    return dict(re.findall(r"(\w+):\s*'([^']*)'", text))


def _js_strings(text: str) -> list[str]:
    """Все строки JS-литерала по порядку: `['a', 'b']` -> ['a', 'b']."""
    return re.findall(r"'([^']*)'", text)


# ── Словари подписей: фолбэк обязан совпадать с Python по КАЖДОМУ ключу ───────────────
@pytest.mark.parametrize("injected, source", [
    # Подписи статусов отклика: то, что написано на бейдже карточки.
    ("STATE_LABELS_PY", chat.STATE_LABELS),
    # Подписи формата работы (Schedule.label) — моста не было вовсе до 08.08.2026,
    # совпадал 1 код из 3, плюс в JS жили мёртвые shift / flyInFlyOut.
    ("SCHED_LABELS_PY", {s.hh_code: s.label for s in Schedule}),
    # Домены порталов в модалке (инцидент 01.08.2026: talanto подписывался как «hh.ru»).
    ("PORTAL_SITES_PY", PORTAL_SITES),
])
def test_js_label_fallback_matches_python_source(injected, source):
    assert _js_dict(_fallback_src(_MODEL_JS, injected)) == source


# ── Наборы кодов: фолбэк-множество обязано совпадать с Python ─────────────────────────
@pytest.mark.parametrize("injected, source", [
    # «Что считается удалёнкой» — один ответ на ленту, отчёты и графики (08.08.2026).
    ("REMOTE_LIKE_PY", set(REMOTE_LIKE_CODES)),
    ("CHAT_FROZEN_PY", set(chat_class.FROZEN_CODES)),
    ("DISCARD_STATES_PY", set(chat.DISCARD_STATES)),
    ("INVITED_STATES_PY", set(chat.INVITED_STATES)),
])
def test_js_code_set_fallback_matches_python_source(injected, source):
    assert set(_js_strings(_fallback_src(_MODEL_JS, injected))) == source


def test_mark_values_fallback_keeps_python_order():
    # У пометок значим ПОРЯДОК: он задаёт порядок кнопок ✓/✕ на карточке (STATUS_BTNS).
    assert _js_strings(_fallback_src(_MODEL_JS, "MARK_VALUES_PY")) == list(MARK_VALUES)


# ── Ключи скоринга опыта — доменные коды, а не подписи ────────────────────────────────
def test_exp_score_is_keyed_by_domain_grade_codes():
    """`resume.js::EXP_SCORE` ключуется кодами `Experience`, и покрыт КАЖДЫЙ грейд.

    Раньше ключами были подписи ('Без опыта', '1–3 года', …) — третья копия
    `config.EXP_LABELS` после конфига и чипов шаблона. Переименование подписи обнуляло бы
    баллы за опыт у всех карточек молча: скоринг тихо переходил на «неизвестный опыт -> 12».
    Незнакомый код (новый член Experience) даст тот же тихий эффект, поэтому набор сверяется
    целиком, а не «все ключи валидны»."""
    literal = _RESUME_JS[_RESUME_JS.index("const EXP_SCORE"):]
    literal = literal[literal.index("{"):literal.index("}") + 1]
    keys = set(re.findall(r"(\w+):\s*\d+", literal))
    assert keys == {e.hh_id for e in Experience}


def test_state_labels_fallback_carries_the_two_keys_that_drifted():
    """Регрессия 08.08.2026: два ключа фолбэка разъехались с `chat.py::STATE_LABELS`.

    Литералы, а не сверка со словарём: этот тест обязан упасть и в том случае, если кто-то
    «выровняет» стороны правкой Python под JS — источник здесь Python, подстраивается JS."""
    fallback = _js_dict(_fallback_src(_MODEL_JS, "STATE_LABELS_PY"))
    assert fallback["PHONE_INTERVIEW"] == "Телефон-интервью"     # было «Звонок»
    assert fallback["DISCARD_VACANCY_CLOSED"] == "Вакансия закрыта"   # было «Закрыта»
