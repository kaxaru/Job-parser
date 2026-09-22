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

По той же причине ярусы стека и множители ролей сверяются с ДЕФОЛТАМИ конфига
(`_RESUME_STACK_DEFAULT`, `_RESUME_ROLE_FIT_DEFAULT`), а не с разрешёнными
`RESUME_STACK_TIERS`/`RESUME_ROLE_FIT`: профиль их перекрывает и в офлайн-копию не попадает
по построению. Страж ловит ровно дрейф КОДА — правку дефолта в Python без правки копии в JS.
Так же `RESUME_LANGS` (выводится из ядра ПРОФИЛЯ) сверяется с пересечением дефолтного ядра
и `LANG_KEYS`, а `RESUME_CORE_SATURATION` — со своим источником в `config` (он не профильный:
дефолт кодовый, `os.getenv` лишь перекрывает его).
"""
import re
from pathlib import Path

import pytest

from hrwork.application.apply.chat import chat, chat_class
from hrwork.application.apply.outcome import APPLY_LABELS
from hrwork.config import (
    _RESUME_DEFAULT,
    _RESUME_LANG_FIT_DEFAULT,
    _RESUME_ROLE_FIT_DEFAULT,
    _RESUME_STACK_DEFAULT,
    _STACK_TIER_WEIGHTS,
    LANG_KEYS,
    PORTAL_SITES,
    RESUME_CORE_SATURATION,
)
from hrwork.domain.employment import Employment
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


def _fallback_expr(js: str, injected: str) -> str:
    """Текст литерала-фолбэка для СКАЛЯРНОЙ константы (`3`, `'bot_interview'`):
    `const X = (typeof X_PY !== 'undefined' && X_PY) || <это>;` -> "<это>".

    `_fallback_src` ищет закрывающую скобку и на скаляре не работает: у числа и строки
    нет `{`/`[`."""
    start = js.index(f"typeof {injected} !==")
    rest = js[js.index("||", start) + 2:]
    return rest[:rest.index(";")].strip()


def _js_dict(text: str) -> dict[str, str]:
    """JS-литерал `{ключ: 'значение', …}` -> dict (значения — строки).

    Ключ бывает голым идентификатором (`applied`) ИЛИ строкой, когда содержит дефис
    (`'no-session'`, коды `TransportStatus` в `APPLY_LABELS`), поэтому разбираем обе формы.
    Голый `(\\w+)` без второй ветви молча пропускал бы `no-session` — и страж сравнивал бы
    9 ключей из 10."""
    out: dict[str, str] = {}
    for quoted, bare, val in re.findall(r"(?:'([\w-]+)'|(\w+)):\s*'([^']*)'", text):
        out[quoted or bare] = val
    return out


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
    # Подписи форм оформления (Employment.label): их видно в модалке и на чипах фильтра.
    ("EMP_LABELS_PY", {e.code: e.label for e in Employment}),
    # Домены порталов в модалке (инцидент 01.08.2026: talanto подписывался как «hh.ru»).
    ("PORTAL_SITES_PY", PORTAL_SITES),
    # Подписи кнопки «Откликнуться в фоне» (`apply/outcome.py::APPLY_LABELS`): ключи — коды
    # `ApplyOutcome` И `TransportStatus`. Дрейф УЖЕ случился (аудит 2026-09-22-quality.md,
    # §3.2): сервер отдавал `taken`, метки для которого в фолбэке не было, — в ленте висело
    # сырое `taken`; `captcha` не был размечен вовсе.
    ("APPLY_LABELS_PY", APPLY_LABELS),
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
    """`resume.js::EXP_FIT` ключуется кодами `Experience`, и покрыт КАЖДЫЙ грейд.

    Раньше ключами были подписи ('Без опыта', '1–3 года', …) — третья копия
    `config.EXP_LABELS` после конфига и чипов шаблона. Переименование подписи обнуляло бы
    баллы за опыт у всех карточек молча: скоринг тихо переходил на «неизвестный опыт».
    Незнакомый код (новый член Experience) даст тот же тихий эффект, поэтому набор сверяется
    целиком, а не «все ключи валидны»."""
    literal = _RESUME_JS[_RESUME_JS.index("const EXP_FIT"):]
    literal = literal[literal.index("{"):literal.index("}") + 1]
    keys = set(re.findall(r"(\w+):\s*[\d.]+", literal))
    assert keys == {e.hh_id for e in Experience}


def _js_num_map(text: str) -> dict[str, float]:
    """JS-литерал `{ключ: число}` -> dict. Ключ бывает голым идентификатором (`Backend: 1`)
    и строкой (`'Data/ML': 0.3`) — разбираем оба вида."""
    out: dict[str, float] = {}
    for m in re.finditer(r"(?:'([^']+)'|([A-Za-zА-Яа-я][\w./-]*))\s*:\s*(-?[\d.]+)", text):
        out[m.group(1) or m.group(2)] = float(m.group(3))
    return out


def test_stack_tiers_fallback_matches_config_default():
    """Ярусы стека в офлайн-копии == дефолт конфига, развёрнутый в веса.

    Сверка именно с ДЕФОЛТОМ, а не с `RESUME_STACK_TIERS`: последний перекрывается
    `resume_profile.json` запускающего, и страж падал бы на чужом резюме вместо дрейфа кода
    (та же причина, по которой не сверяется RESUME_CORE — см. шапку модуля)."""
    expected = {tech: weight
                for tier, weight in _STACK_TIER_WEIGHTS.items()
                for tech in _RESUME_STACK_DEFAULT[tier]}
    assert _js_num_map(_fallback_src(_RESUME_JS, "RESUME_TIERS_PY")) == expected


def test_lang_keys_fallback_matches_the_domain_set():
    """Что вообще СЧИТАЕТСЯ языком — константа кода (`config.LANG_KEYS`), не профиля,
    поэтому сверяется целиком. Разойдись копия — ось языка начала бы принимать за «чужой»
    то, чего в наборе нет, и наоборот."""
    assert set(_js_strings(_fallback_src(_RESUME_JS, "LANG_KEYS_PY"))) == set(LANG_KEYS)


def test_lang_fit_fallback_matches_config_default():
    """Множители оси языка. Средний исход («язык не назван» -> 0.5) отличать от «чужой»
    (0.1) обязательно: у части вакансий стек в тексте не перечислен вовсе."""
    assert _js_num_map(_fallback_src(_RESUME_JS, "RESUME_LANG_FIT_PY")) == _RESUME_LANG_FIT_DEFAULT


def test_role_fit_fallback_matches_config_default():
    """Множители ролей в офлайн-копии == дефолт конфига.

    Это ось ЖЕЛАНИЯ, и её расхождение тихое вдвойне: балл поедет, а состав ленты нет —
    заметить можно только по порядку карточек."""
    assert _js_num_map(_fallback_src(_RESUME_JS, "RESUME_ROLE_FIT_PY")) == _RESUME_ROLE_FIT_DEFAULT


def test_resume_langs_fallback_matches_the_default_profile_anchor():
    """`resume.js::RESUME_LANGS` выводится из ядра ПРОФИЛЯ (`RESUME_CORE ∩ LANG_KEYS`),
    поэтому офлайн-копия сверяется с пересечением ДЕФОЛТНОГО ядра и `LANG_KEYS`, а не с
    разрешённым значением: у форка со своим `resume_profile.json` сверка падала бы на чужой
    настройке, а не на дрейфе кода (та же причина, по которой не сверяется RESUME_CORE —
    см. шапку модуля)."""
    expected = [t for t in _RESUME_DEFAULT["core"] if t in LANG_KEYS]
    assert _js_strings(_fallback_src(_RESUME_JS, "RESUME_LANGS_PY")) == expected


def test_core_saturation_fallback_matches_the_config_source():
    """Порог насыщения ядра (`config.RESUME_CORE_SATURATION`) — офлайн-копия обязана
    совпадать с источником в Python. Не профильная константа (дефолт кодовый, `os.getenv`
    лишь перекрывает его), поэтому сверяем с самим `config`, а не с дефолтом отдельной
    константы. Правит БАЛЛ карточки, а не состав ленты -> дрейф заметили бы не сразу
    (аудит 2026-09-22, §4-40)."""
    assert _fallback_expr(_RESUME_JS, "RESUME_CORE_SAT_PY") == str(RESUME_CORE_SATURATION)


def test_bot_interview_code_fallback_matches_python():
    """Офлайн-копия кода ЖЁЛТОГО фриза == источник в Python (`ChatKind.BOT_INTERVIEW`).

    Набор `CHAT_FROZEN_PY` отвечает лишь «тупик»: переименуй код в `chat_class`, набор
    обновится, а литерал в JS нет — и жёлтый тон карточки/бейджа пропал бы молча
    (аудит 2026-09-22, §3.2)."""
    code = chat_class.ChatKind.BOT_INTERVIEW.code
    assert _fallback_expr(_MODEL_JS, "CHAT_BOT_INTERVIEW_PY") == f"'{code}'"


def test_state_labels_fallback_carries_the_two_keys_that_drifted():
    """Регрессия 08.08.2026: два ключа фолбэка разъехались с `chat.py::STATE_LABELS`.

    Литералы, а не сверка со словарём: этот тест обязан упасть и в том случае, если кто-то
    «выровняет» стороны правкой Python под JS — источник здесь Python, подстраивается JS."""
    fallback = _js_dict(_fallback_src(_MODEL_JS, "STATE_LABELS_PY"))
    assert fallback["PHONE_INTERVIEW"] == "Телефон-интервью"     # было «Звонок»
    assert fallback["DISCARD_VACANCY_CLOSED"] == "Вакансия закрыта"   # было «Закрыта»
