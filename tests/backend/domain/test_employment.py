"""Форма оформления (`domain/employment.py`): коды, подписи, парсеры, детектор по тексту.

Ожидаемые значения — из словаря спецификации «ТК РФ/РБ, самозанятый, ИП, ГПХ; формы может
быть несколько; не сказано — значит не сказано», а не из реализации.
"""
import pytest

from hrwork.domain.employment import EMPLOYMENT_SIG, Employment, detect_employment


@pytest.mark.parametrize("form, code, label", [
    (Employment.LABOR_CODE, "labor_code", "ТК РФ/РБ"),
    (Employment.SELF_EMPLOYED, "self_employed", "Самозанятый"),
    (Employment.SOLE_TRADER, "sole_trader", "ИП"),
    (Employment.CIVIL_CONTRACT, "civil_contract", "ГПХ"),
])
def test_each_form_has_its_code_and_human_label(form, code, label):
    assert form.code == code
    assert form.label == label


def test_the_domain_knows_exactly_four_forms():
    # Список закрыт фильтром hh: ТК (РФ или РБ), самозанятость, ИП, ГПХ. B2B/аутстафф
    # сюда НЕ входят — замер 10.08.2026 показал, что на западных порталах это слово
    # означает тип занятости, а не форму оформления.
    assert [f.code for f in Employment] == [
        "labor_code", "self_employed", "sole_trader", "civil_contract"]


@pytest.mark.parametrize("code, expected", [
    ("labor_code", Employment.LABOR_CODE),
    ("civil_contract", Employment.CIVIL_CONTRACT),
    ("", None),
    (None, None),
    ("garbage", None),
])
def test_from_code_is_a_lenient_parser(code, expected):
    # мягкий контракт слово в слово как у Schedule.from_code: VO | None, без выдумывания
    assert Employment.from_code(code) is expected


def test_from_label_is_a_strict_parser():
    # строгий контракт: наши подписи — roundtrip, чужая подпись роняет сразу
    assert Employment.from_label("ТК РФ/РБ") is Employment.LABOR_CODE
    assert Employment.from_label("Самозанятый") is Employment.SELF_EMPLOYED
    with pytest.raises(ValueError):
        Employment.from_label("Подряд")


@pytest.mark.parametrize("text, expected", [
    ("Оформление по ТК РФ с первого дня", (Employment.LABOR_CODE,)),
    ("официальное оформление по ТК РБ", (Employment.LABOR_CODE,)),
    ("Трудоустройство в соответствии с Трудовым кодексом", (Employment.LABOR_CODE,)),
    ("оформление по Трудовому договору", (Employment.LABOR_CODE,)),
    ("Официальное трудоустройство, белая зарплата", (Employment.LABOR_CODE,)),
    ("Оформление: ИП с оплатой раз в месяц", (Employment.SOLE_TRADER,)),
    ("оформление по ИП", (Employment.SOLE_TRADER,)),
    ("работа с индивидуальным предпринимателем", (Employment.SOLE_TRADER,)),
    ("Оплата услуг самозанятым исполнителям", (Employment.SELF_EMPLOYED,)),
    ("Оформление по договору ГПХ", (Employment.CIVIL_CONTRACT,)),
    ("гражданско-правовой договор", (Employment.CIVIL_CONTRACT,)),
])
def test_detector_names_the_form_mentioned_in_the_text(text, expected):
    assert detect_employment(text) == expected


@pytest.mark.parametrize("text, expected", [
    # «или как самозанятый» — обычная формулировка: вакансия обязана попасть в ОБА фильтра
    ("Оформление по ТК РФ или как самозанятый",
     (Employment.LABOR_CODE, Employment.SELF_EMPLOYED)),
    ("Оформление по самозанятости/ИП",
     (Employment.SELF_EMPLOYED, Employment.SOLE_TRADER)),
    ("Работаем с самозанятыми и ИП, возможно совмещение",
     (Employment.SELF_EMPLOYED, Employment.SOLE_TRADER)),
    ("Оформление: договор ГПХ с самозанятым, ИП или физлицом, трудовой договор",
     (Employment.LABOR_CODE, Employment.SELF_EMPLOYED,
      Employment.SOLE_TRADER, Employment.CIVIL_CONTRACT)),
])
def test_detector_returns_every_offered_form_in_declaration_order(text, expected):
    # порядок КАНОНИЧЕСКИЙ (порядок членов enum), а не порядок появления в тексте:
    # результат кешируется и сравнивается на равенство
    assert detect_employment(text) == expected


@pytest.mark.parametrize("text", [
    # Реквизиты работодателя: «ИП» здесь — форма КОМПАНИИ, а не предложение исполнителю
    "О компании ИП Ляпин (4 света) — ведущий дистрибьютор световых решений",
    # НПД в стройке = нормативно-правовая документация, а не налог на профдоход
    "Вести календарные графики по полному циклу НПД",
    # Обязанность инженера-строителя, а не форма оформления вакансии
    "Подготовка РД для проведения конкурсов и заключения договоров подряда",
    # Реклама кадрового агентства — обещание помощи, а не оформление по ТК
    "Поможем с трудоустройством и составлением резюме",
    "",
])
def test_detector_stays_silent_when_the_text_is_not_about_hiring_form(text):
    assert detect_employment(text) == ()


def test_signature_changes_when_the_dictionary_changes():
    # кеш форм в raw протухает по этой сигнатуре; она обязана быть коротким hex-токеном
    assert len(EMPLOYMENT_SIG) == 12
    assert EMPLOYMENT_SIG.lower() == EMPLOYMENT_SIG
    int(EMPLOYMENT_SIG, 16)


# ── Структурное поле hh (разведка 10.08.2026) ────────────────────────────────────────
# `acceptLaborContract` + `civilLawContracts` — ровно то, что стоит за фильтром
# «Оформление» на hh.ru. Коды взяты из живого состояния страницы поиска, не выдуманы:
# SELF_EMPLOYED (276 вакансий замера), INDIVIDUAL_ENTREPRENEUR (210), INDIVIDUAL_PERSON (235).

def _civil(*codes):
    """Форма, в которой hh отдаёт список: [{"civilLawContractsElement": [...]}]."""
    return [{"civilLawContractsElement": list(codes)}]


@pytest.mark.parametrize("labor, civil, expected", [
    (True, None, (Employment.LABOR_CODE,)),
    (False, _civil("SELF_EMPLOYED"), (Employment.SELF_EMPLOYED,)),
    (False, _civil("INDIVIDUAL_ENTREPRENEUR"), (Employment.SOLE_TRADER,)),
    # физлицо = договор ГПХ; на самом hh.ru эта галочка так и подписана
    (False, _civil("INDIVIDUAL_PERSON"), (Employment.CIVIL_CONTRACT,)),
    (True, _civil("SELF_EMPLOYED", "INDIVIDUAL_ENTREPRENEUR"),
     (Employment.LABOR_CODE, Employment.SELF_EMPLOYED, Employment.SOLE_TRADER)),
])
def test_hh_fields_map_to_forms(labor, civil, expected):
    assert Employment.from_hh_fields(labor, civil) == expected


@pytest.mark.parametrize("labor, civil", [
    (False, None),
    (False, []),
    (False, [{}]),                       # список есть, элементов нет — работодатель не заполнил
    (None, None),
])
def test_hh_fields_unfilled_mean_not_stated(labor, civil):
    # 444 из 712 вакансий замера — именно этот случай; это «не сказано», а не «нет оформления»
    assert Employment.from_hh_fields(labor, civil) == ()


def test_hh_field_with_an_unknown_code_does_not_break_collection():
    # hh волен завести пятый код: незнакомый молча пропускается, знакомый рядом — берётся
    assert Employment.from_hh_fields(False, _civil("SELF_EMPLOYED", "MARTIAN_CONTRACT")) == (
        Employment.SELF_EMPLOYED,)
