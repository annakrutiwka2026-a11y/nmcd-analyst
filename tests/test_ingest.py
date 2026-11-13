"""Разбор ТЗ по правилам: три реальных ТЗ и описание текстом."""
from decimal import Decimal
from pathlib import Path

from app.ingest.reader import read_input
from app.ingest.rules_parser import parse_procurement, parse_requirement
from app.models import Op

EX = Path(__file__).resolve().parent.parent / "examples" / "tz"


def parse(name: str):
    return parse_procurement(read_input((EX / name).read_bytes(), name))


def test_paper():
    p = parse("tz_paper.docx")
    assert [it.quantity for it in p.items] == [Decimal("2500"), Decimal("25"), Decimal("50")]
    assert all(it.unit == "пачка" for it in p.items)
    a4 = p.items[0]
    assert a4.brand == "SvetoCopy"
    assert a4.okpd2 == "17.12.14.160"
    assert a4.content is not None and a4.content.value == Decimal("500")
    density = next(r for r in a4.requirements if "Масса" in r.name)
    assert density.op == Op.EQ and density.value == "80" and density.tolerance == "±3"
    assert p.delivery_included is True


def test_printer():
    p = parse("tz_3d_printer.docx")
    it = p.items[0]
    assert it.brand.lower() == "creatbot" and it.model == "D600 PRO"
    assert it.quantity == Decimal("1")
    assert any("Технология" in r.name for r in it.requirements)


def test_cytokines():
    p = parse("tz_cytokines.docx")
    assert len(p.items) == 5
    contents = [(it.content.value, it.content.unit, it.content_op) for it in p.items]
    assert contents[0] == (Decimal("10"), "мкг", Op.GE)
    assert contents[3] == (Decimal("100"), "мкг", Op.GE)
    assert [it.quantity for it in p.items] == [Decimal(2), Decimal(1), Decimal(1), Decimal(2), Decimal(1)]


def test_requirement_conditions():
    assert parse_requirement("r", "Чистота", "более 97", "%").op == Op.GT
    r = parse_requirement("r", "ED50", "более 1 и менее 1,6", "нг/мл")
    assert r.op == Op.BETWEEN and (r.value, r.value_max) == ("1", "1.6")
    r = parse_requirement("r", "Хранение", "не выше - 20 и не ниже -70", "°C")
    assert r.op == Op.BETWEEN and (r.value, r.value_max) == ("-70", "-20")
    assert parse_requirement("r", "Масса", "не менее 10", "мкг").op == Op.GE
    assert parse_requirement("r", "Сенсорный дисплей", "наличие").op == Op.PRESENT


def test_plain_text():
    doc = read_input("Бумага офисная А4 80 г/м2 — 100 пачек".encode(), "описание.txt")
    p = parse_procurement(doc)
    assert len(p.items) == 1
    assert p.items[0].quantity == Decimal("100")


def test_plain_text_description():
    from app.ingest.reader import read_input
    from app.ingest.rules_parser import parse_procurement
    text = ("Закупка картриджей для принтеров\n\n"
            "Картридж лазерный HP 59A (CF259A), оригинальный, черный, ресурс не менее 3000 страниц — 10 шт.")
    it = parse_procurement(read_input(text.encode(), "opisanie.txt")).items[0]
    assert it.name == "Картридж лазерный HP 59A (CF259A)" and it.quantity == 10 and it.unit == "шт."
    assert it.brand == "HP" and it.model == "59A / CF259A"
    reqs = {r.name: r for r in it.requirements}
    assert reqs["Оригинальность"].mandatory and not reqs["Оригинальность"].by_brand
    assert reqs["ресурс"].value == "3000" and reqs["ресурс"].unit == "страниц" and reqs["ресурс"].by_brand

    paper = parse_procurement(read_input("Бумага для офисной техники А4, 80 г/м², 500 листов в пачке, марка C — 2500 пачек"
                                         .encode(), "opisanie.txt")).items[0]
    assert paper.quantity == 2500 and paper.unit == "пачка" and paper.content.value == 500
    names = {r.name: r.value for r in paper.requirements}
    assert names["Формат"] == "А4" and names["Плотность"] == "80" and names["марка"] == "C"


def test_attribute_written_into_the_name():
    """«Картридж HP 59A (CF259A) оригинальный» без запятой: оригинальность — отдельное существенное требование."""
    text = "Картридж HP 59A (CF259A) оригинальный, ресурс 3000 страниц — 4 шт."
    it = parse_procurement(read_input(text.encode(), "описание.txt")).items[0]
    assert it.name == "Картридж HP 59A (CF259A)" and it.model == "59A / CF259A" and it.quantity == Decimal("4")
    origin = next(r for r in it.requirements if r.name == "Оригинальность")
    assert origin.mandatory and not origin.by_brand
    it = parse_procurement(read_input("Картридж HP 59A черный — 2 шт.".encode(), "описание.txt")).items[0]
    assert it.name == "Картридж HP 59A" and [r.name for r in it.requirements] == ["Цвет"]
