"""Извлечение полей формы (RFC-003) на утином мок-page. Структура выверена живым прогоном HH:
[data-qa=task-body] = вопрос + группа radio/checkbox с подписями-cell (+ value для отметки).

Второе защищаемое свойство — ПОЛНОТА СЪЁМА: сколько вопросов на странице, столько полей
должно быть снято, а несъём обязан быть ВИДЕН вызывающему (`FormExtract.missed`), иначе
инвариант «шлём только при полноте» проходит по обрезанному списку."""
from hrwork.application.apply.forms.form_read import FieldType, extract_fields, extract_form


class _Loc:
    """Утиный локатор Playwright: all()/get_attribute()/inner_text()/count()/first/locator()."""
    def __init__(self, items=(), attrs=None, text="", sub=None):
        self._items = list(items)
        self._attrs = attrs or {}
        self._text = text
        self._sub = sub or {}

    def all(self):
        return self._items

    def get_attribute(self, n):
        return self._attrs.get(n)

    def inner_text(self):
        return self._text

    def count(self):
        return len(self._items) if self._items else (1 if (self._text or self._attrs) else 0)

    @property
    def first(self):
        return self._items[0] if self._items else self

    def locator(self, sel):
        return self._sub.get(sel, _Loc())


class _Page:
    def __init__(self, mapping):
        self._m = mapping

    def locator(self, sel):
        return self._m.get(sel, _Loc())


def _radio(name, value):
    return _Loc(attrs={"name": name, "value": value})


def _body(question, radios=(), checks=(), cells=()):
    return _Loc(sub={
        '[data-qa="task-question"]': _Loc([_Loc(text=question)]),
        'input[type="radio"]': _Loc(list(radios)),
        'input[type="checkbox"]': _Loc(list(checks)),
        '[data-qa="cell"]': _Loc([_Loc(text=t) for t in cells]),
    })


def test_task_radio_captures_labels_and_values():
    body = _body("Готов к офису?", radios=[_radio("task_1", "a"), _radio("task_1", "open")],
                 cells=["Да", "Свой вариант"])
    fields = extract_fields(_Page({'[data-qa="task-body"]': _Loc([body])}))
    assert len(fields) == 1
    f = fields[0]
    assert f.ftype is FieldType.RADIO and f.name == "task_1" and f.prompt == "Готов к офису?"
    assert f.options == ("Да", "Свой вариант") and f.opt_values == ("a", "open")
    assert f.selector == 'input[name="task_1"]'


def test_task_checkbox_when_no_radio():
    body = _body("Часов в неделю?", checks=[_radio("task_2", "20"), _radio("task_2", "30")],
                 cells=["20", "30"])
    f = extract_fields(_Page({'[data-qa="task-body"]': _Loc([body])}))[0]
    assert f.ftype is FieldType.CHECKBOX and f.options == ("20", "30") and f.opt_values == ("20", "30")


def test_missing_cell_label_falls_back_to_value():
    body = _body("Q", radios=[_radio("task_3", "x")], cells=[])       # подписи нет -> value
    f = extract_fields(_Page({'[data-qa="task-body"]': _Loc([body])}))[0]
    assert f.options == ("x",)


def test_empty_form_yields_nothing():
    assert extract_fields(_Page({})) == []


def test_generic_select_options():
    sel = _Loc(attrs={"name": "grade"},
               sub={"option": _Loc([_Loc(text="Junior"), _Loc(text="Middle"), _Loc(text="  ")])})
    f = extract_fields(_Page({"select": _Loc([sel])}))[0]
    assert f.ftype is FieldType.SELECT and f.options == ("Junior", "Middle")


# ── полнота съёма: несъём виден, а не прячется обрезанным списком ──
# ДЕФЕКТ 08.08.2026: `contextlib.suppress` охватывал ВЕСЬ цикл по вопросам. Исключение на
# пятом вопросе возвращало четыре поля, try_autofill резолвил 4 из 4, гейт полноты проходил
# и анкета уходила работодателю с дырами — причина в логах не видна.
class _BrokenLoc(_Loc):
    """Флэки-DOM: локатор отвалился посреди съёма (Playwright так и делает)."""
    def locator(self, sel):
        raise RuntimeError("Element is not attached to the DOM")


def _radio_body(name, question="Готов к офису?"):
    return _body(question, radios=[_radio(name, "y"), _radio(name, "n")], cells=["Да", "Нет"])


def test_full_form_is_marked_complete():
    page = _Page({'[data-qa="task-body"]': _Loc([_radio_body("task_1"), _radio_body("task_2")])})
    got = extract_form(page)
    assert [f.name for f in got.fields] == ["task_1", "task_2"]
    assert (got.missed, got.complete) == (0, True)


def test_broken_question_keeps_the_rest_and_marks_form_incomplete():
    page = _Page({'[data-qa="task-body"]': _Loc([_radio_body("task_1"), _BrokenLoc(),
                                                 _radio_body("task_3")])})
    got = extract_form(page)
    assert [f.name for f in got.fields] == ["task_1", "task_3"]   # съём соседей не оборвался
    assert (got.missed, got.complete) == (1, False)               # но анкета неполна -> не шлём


def test_question_without_a_known_control_is_counted_as_missed():
    # вопрос на странице есть, а заполняемого контрола мы не распознали — это тоже пробел
    page = _Page({'[data-qa="task-body"]': _Loc([_radio_body("task_1"), _body("Загрузите файл")])})
    got = extract_form(page)
    assert [f.name for f in got.fields] == ["task_1"]
    assert (got.missed, got.complete) == (1, False)


def test_two_questions_on_one_selector_are_counted_as_missed():
    # дедуп по селектору снимает второй вопрос молча: заполнив одну группу, вторую не закроешь
    page = _Page({'[data-qa="task-body"]': _Loc([_radio_body("task_1", "Опыт?"),
                                                 _radio_body("task_1", "Готовы?")])})
    got = extract_form(page)
    assert [f.prompt for f in got.fields] == ["Опыт?"]
    assert (got.missed, got.complete) == (1, False)


def test_unreadable_page_is_incomplete_not_empty():
    # страница не отдала список вопросов: сколько их было — неизвестно, значит съём неполон
    got = extract_form(_BrokenLoc())
    assert got.fields == ()
    assert (got.missed, got.complete) == (2, False)   # по одному несъёму на tasks и selects
