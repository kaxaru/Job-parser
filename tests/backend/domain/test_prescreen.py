"""Страж эквивалентности подстрочного пресина в _detect_techs (п.12 docs/history/ddd_fix.md).

`_detect_techs` для скорости (17× на 34k) пропускает regex, если ни один ГАРАНТИРОВАННЫЙ
литерал паттерна не встретился в тексте (`_prescreen_literal`/`_build_screened`). Это верно,
только если извлечённый литерал — действительно необходимое условие матча. Правка TECH_PATTERNS,
ломающая извлечение (литерал за опциональным квантификатором, в альтернации и т.п.), дала бы
ТИХИЙ false-negative: пресин отсёк бы текст, который regex на деле матчит.

Тест ловит это: `_detect_techs` (со скипом) обязан = наивному прогону ВСЕХ regex (без скипа).
"""
import os
import random
import re

import pytest

from hrwork.config import RAW_FILE, TECH_PATTERNS
from hrwork.domain.parsing import _SCREENED, _detect_techs, _strip_device_req


def _naive_detect_techs(text: str) -> list[str]:
    """Эталон: та же предобработка, что в _detect_techs, но БЕЗ пресин-скипа — гоним все regex."""
    t = _strip_device_req(text.lower())
    return [tech for tech, rx, _screen in _SCREENED if rx.search(t)]


def test_screened_covers_every_tech_of_the_dictionary():
    """Страж СОСТАВА, без которого эквивалентность ниже проверяет продукт сам с собой.

    АУДИТ 09.08.2026: и эталон, и проверяемый `_detect_techs` идут по одному и тому же
    предпосчитанному `_SCREENED`. Потеряй `_build_screened` тех (фильтр «if not pat:
    continue», сломанная компиляция одного паттерна) — обе стороны потеряют его одинаково,
    все 510 случаев останутся зелёными, а тег перестанет детектиться в проде. Здесь одна
    сторона — литерал: объём словаря стека назван числом в docs/testing.md."""
    assert [tech for tech, _rx, _screen in _SCREENED] == list(TECH_PATTERNS)
    assert len(_SCREENED) == 55        # объём словаря стека, docs/testing.md


# Словарь фрагментов: вытаскиваем литеральные куски из САМИХ паттернов (авто-покрытие всех
# техов, включая будущие) + шумовые слова + device-клауза (проверить взаимодействие со
# _strip_device_req) + тексты-ловушки на lookaround (go/java/c#/1с/.net).
_META = re.compile(r"[|()\[\]{}?*+.^$\\]")
_FRAGS = sorted({
    stripped
    for pat in TECH_PATTERNS.values()
    for chunk in _META.split(pat)
    if len(stripped := chunk.strip()) >= 2
})
_NOISE = [
    "опыт", "знание", "разработчик", "требуется", "работа", "инженер", "команда",
    "using", "backend", "стек", "язык", "нейросеть", "яндекс go", "java script",
    "go developer", "c# developer", "node js", "sql server", "google cloud",
    "смартфон или планшет с ос ios или android", "work from home", "удалённо",
]
_VOCAB = _FRAGS + _NOISE


def test_prescreen_equivalent_on_generated_corpus():
    # Детерминированный корпус: случайные комбинации фрагментов+шума (seed -> воспроизводимо в CI).
    # ИСКЛЮЧЕНИЕ из запрета циклов (docs/testing.md): это property-тест, случай генерируется,
    # а не перечисляется — параметризовать нечего. Виновный текст называет repr в сообщении.
    rnd = random.Random(20260718)
    for _ in range(2000):
        k = rnd.randint(0, 8)
        text = " ".join(rnd.choice(_VOCAB) for _ in range(k))
        assert _detect_techs(text) == _naive_detect_techs(text), repr(text)


@pytest.mark.parametrize("wrap", ["{}", "опыт {} разработки", "{}, python", "[{}]"])
@pytest.mark.parametrize("frag", _VOCAB)
def test_prescreen_equivalent_on_each_fragment_alone(frag, wrap):
    # Каждый фрагмент отдельно + в тривиальной обёртке — прямое давление на границы \b/lookaround.
    # Перечислимый набор -> параметризация: падение называет фрагмент И обёртку, остальные
    # 500+ случаев продолжают проверяться (цикл останавливался на первом).
    text = wrap.format(frag)
    assert _detect_techs(text) == _naive_detect_techs(text)


@pytest.mark.slow
@pytest.mark.skipif(not os.getenv("PRESCREEN_FULL"),
                    reason="тяжёлый свеп по всему raw — opt-in: PRESCREEN_FULL=1 pytest")
def test_prescreen_equivalent_on_real_data():
    # Сильнейший сигнал: ВСЕ реальные тайтлы+сниппеты. Медленный (наив гоняет все 55 regex),
    # потому opt-in; генеративный+пофрагментный тесты выше — быстрый CI-страж на каждый прогон.
    # ИСКЛЮЧЕНИЕ из запрета циклов (docs/testing.md): набор случаев — весь корпус на диске
    # (~90k записей), параметризовать его нельзя; виновную вакансию называет id в сообщении.
    import json

    if not RAW_FILE.exists():
        pytest.skip("нет data/vacancies_raw.json")
    raw = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    for d in raw:
        snippet = (d.get("snippet") or {})
        text = " ".join(filter(None, [
            d.get("name", ""), snippet.get("requirement", ""), snippet.get("responsibility", ""),
        ]))
        assert _detect_techs(text) == _naive_detect_techs(text), d.get("id")
