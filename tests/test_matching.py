"""Сверка: характеристики реестра ЕИС, транслитерация, модификации модели, габариты, значения из названия."""
from datetime import datetime, timezone
from decimal import Decimal

from app.engine.matching import match_offer, model_match, parse_bounds
from app.engine.rules import load_rules
from app.engine.units import split_value_unit
from app.models import CheckStatus, Item, MatchClass, Offer, OfferChar, Op, Requirement, SourceKind

RULES = load_rules()
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def req(rid, name, op, value=None, **kw):
    return Requirement(id=rid, name=name, op=op, value=value, **kw)


def eis_offer(title, pairs, price="300"):
    chars = []
    for name, raw in pairs:
        value, unit = split_value_unit(name, raw)
        chars.append(OfferChar(name=name, value=value, unit=unit, quote=f"{name}: {raw}", quote_verified=True))
    text = f"{title}. Характеристики: " + "; ".join(f"{a}: {b}" for a, b in pairs)
    return Offer(id="e1", item_id="i1", source=SourceKind.EIS44, source_name="ЕИС", url="https://zakupki.gov.ru/x",
                 retrieved_at=NOW, title=title, price=Decimal(price), price_unit="Пачка", characteristics=chars,
                 text=text)


def web_offer(title, text="", chars=()):
    return Offer(id="w1", item_id="i1", source=SourceKind.WEB, source_name="shop.ru", url="https://shop.ru/p",
                 retrieved_at=NOW, title=title, price=Decimal("1000"), price_unit="шт.", text=text,
                 characteristics=list(chars))


PAPER = Item(id="i1", number=1, name="Бумага для офисной техники А4 SvetoCopy", brand="SvetoCopy", quantity=Decimal(10),
             unit="пачка", requirements=[
                 req("r1", "Вид бумаги", Op.EQ, "для офисной техники", mandatory=True),
                 req("r2", "Формат", Op.EQ, "А4", mandatory=True),
                 req("r3", "Марка бумаги", Op.EQ, "С", mandatory=True, by_brand=True),
                 req("r4", "Масса листа", Op.EQ, "80", unit="г/м²", tolerance="±3", mandatory=True, by_brand=True),
                 req("r5", "Белизна CIE", Op.EQ, "146", unit="%", tolerance="±3", mandatory=False)])


def test_ktru_values_are_split():
    assert split_value_unit("Масса бумаги площадью 1м2", "80Грамм") == ("80", "г/м²")
    assert split_value_unit("Количество листов в пачке", "500Штука") == ("500", "шт")
    assert split_value_unit("Формат", "А4") == ("А4", None)
    b = parse_bounds("≥ 160 и < 180", "г/м²")
    assert (b.lo, b.hi) == (Decimal(160), Decimal(180))
    assert parse_bounds("10 мкг, 50 мкг") is None          # перечисление, не диапазон
    assert parse_bounds("Tween20") is None


def test_eis_paper_without_model_is_identical():
    o = eis_offer("Бумага для офисной техники. Товарный знак: SvetoCopy",
                  [("Масса бумаги площадью 1м2", "80Грамм"), ("Формат", "А4"), ("Марка бумаги", "Не ниже С"),
                   ("Количество листов в пачке", "500Штука"), ("Цветность", "Белая")])
    m = match_offer(PAPER, o, RULES)
    st = {c.requirement_id: c.status for c in m.checks}
    assert st["r1"] == CheckStatus.OK            # из названия позиции, а не из «Масса бумаги»
    assert st["r4"] == CheckStatus.OK            # 80 Грамм у «площадью 1м2» — это г/м²
    assert m.cls == MatchClass.IDENTICAL


def test_eis_other_density_is_unsuitable():
    o = eis_offer("Бумага для офисной техники. Товарный знак: SvetoCopy",
                  [("Масса бумаги площадью 1м2", "≥ 160 и < 180 Грамм"), ("Формат", "А4")])
    m = match_offer(PAPER, o, RULES)
    assert m.cls == MatchClass.UNSUITABLE and not m.not_confirmed


def test_characteristics_defined_by_model_only():
    # знак и модель названы: «марка C» в источнике не указана, но задана моделью «Classic»
    classic = PAPER.model_copy(update={"model": "Classic"})
    o = web_offer("Бумага SvetoCopy Classic A4, 500 л., 80 г/м², для офисной техники")
    m = match_offer(classic, o, RULES)
    st = {c.requirement_id: (c.status, c.note) for c in m.checks}
    assert st["r4"][0] == CheckStatus.OK and "назван" in st["r4"][1]
    assert st["r3"] == (CheckStatus.OK, "задано товарным знаком и моделью из ТЗ")
    assert m.cls == MatchClass.IDENTICAL
    # один товарный знак без модели характеристик не задаёт: марка не подтверждена — только однородное
    m2 = match_offer(PAPER, web_offer("Бумага SvetoCopy A4, 500 л., 80 г/м², для офисной техники"), RULES)
    assert m2.cls == MatchClass.HOMOGENEOUS
    # другой знак — исключение (две существенные не подтверждены)
    o3 = web_offer("Бумага Снегурочка А4, 500 л., 80 г/м², для офисной техники")
    assert match_offer(PAPER, o3, RULES).not_confirmed


def test_transliteration_3d():
    it = Item(id="i1", number=1, name="3D принтер", quantity=Decimal(1), unit="шт.",
              requirements=[req("r1", "Тип принтера", Op.EQ, "3D принтер", mandatory=True)])
    m = match_offer(it, web_offer("3Д принтер CreatBot"), RULES)
    assert m.checks[0].status == CheckStatus.OK


def test_model_modification_is_not_the_model():
    assert model_match("D600 PRO", "3D принтер CreatBot D600 Pro") is True
    assert model_match("D600 PRO", "CreatBot D600PRO купить") is True
    assert model_match("D600 PRO", "CreatBot D600 Pro, 2 экструдера") is True
    assert model_match("D600 PRO", "3D принтер CreatBot D600 Pro 2 HS") is False
    assert model_match("D600 PRO", "CreatBot D600 Pro HS") is False


def test_print_volume_triple():
    it = Item(id="i1", number=1, name="3D принтер", model="D600 PRO", brand="CreatBot", quantity=Decimal(1),
              unit="шт.", requirements=[req("r1", "Область печати", Op.EQ, "600х600х600", unit="мм", mandatory=True)])
    ok = web_offer("CreatBot D600 Pro", chars=[OfferChar(name="Область печати", value="600 x 600 x 600 мм")])
    bad = web_offer("CreatBot D600 Pro", chars=[OfferChar(name="Область печати", value="620*620*620 мм")])
    assert match_offer(it, ok, RULES).checks[0].status == CheckStatus.OK
    assert match_offer(it, bad, RULES).checks[0].status == CheckStatus.FAIL


def test_text_requirement_not_paired_with_number():
    # «Вид бумаги» не сопоставляется с числом «80 г/м²» из характеристики «Масса бумаги»
    o = eis_offer("Бумага. Товарный знак: SvetoCopy", [("Масса бумаги площадью 1м2", "80Грамм")])
    m = match_offer(PAPER, o, RULES)
    c = next(c for c in m.checks if c.requirement_id == "r1")
    assert c.status == CheckStatus.MISSING and c.offer_value is None


def test_value_precision_from_tz():
    # «15 кДа» в ТЗ записано с точностью до целых: 15,5 кДа ещё совпадает, 16 кДа — уже нет
    it = Item(id="i1", number=1, name="Интерлейкин-2", quantity=Decimal(1), unit="шт.",
              requirements=[req("r1", "Молекулярная масса", Op.EQ, "15", unit="кДа", mandatory=True)])
    ok = web_offer("rhIL-2", chars=[OfferChar(name="Молекулярная масса", value="15,5 кДа")])
    bad = web_offer("rhIL-2", chars=[OfferChar(name="Молекулярная масса", value="16 кДа")])
    assert match_offer(it, ok, RULES).checks[0].status == CheckStatus.OK
    assert match_offer(it, bad, RULES).checks[0].status == CheckStatus.FAIL


def test_russian_word_forms():
    it = Item(id="i1", number=1, name="Принтер", quantity=Decimal(1), unit="шт.",
              requirements=[req("r1", "Состояние товара", Op.EQ, "новый", mandatory=False),
                            req("r2", "Диаметры сопел", Op.IN, values=["0.4", "0.6"], unit="мм", mandatory=False)])
    o = web_offer("Принтер", chars=[OfferChar(name="Состояние товара", value="Новое"),
                                    OfferChar(name="Диаметры сопел", value="0,4 мм")])
    st = [c.status for c in match_offer(it, o, RULES).checks]
    assert st == [CheckStatus.OK, CheckStatus.OK]


def test_one_unconfirmed_essential_is_homogeneous():
    # подтверждены формат и плотность, марка в источнике не указана — однородное, а не исключение
    paper = PAPER.model_copy(update={"requirements": [
        req("r2", "Формат", Op.EQ, "А4", mandatory=True),
        req("r3", "Марка бумаги", Op.EQ, "С", mandatory=True),
        req("r4", "Масса листа", Op.EQ, "80", unit="г/м²", tolerance="±3", mandatory=True)]})
    o = eis_offer("Бумага для офисной техники. Товарный знак: SvetoCopy",
                  [("Масса бумаги площадью 1м2", "80Грамм"), ("Формат", "А4")])
    m = match_offer(paper, o, RULES)
    assert m.cls == MatchClass.HOMOGENEOUS and any("не указано" in r for r in m.reasons)
    # подтверждена одна из двух — исключение
    two = paper.model_copy(update={"requirements": paper.requirements[:2]})
    assert match_offer(two, o, RULES).not_confirmed


def test_technology_code_inside_description():
    it = Item(id="i1", number=1, name="3D принтер", quantity=Decimal(1), unit="шт.",
              requirements=[req("r1", "Технология печати", Op.CONTAINS,
                                "FDM (Fused Deposition Modeling) Моделирование методом послойного наплавления",
                                mandatory=True)])
    o = web_offer("Принтер", chars=[OfferChar(name="Технология печати", value="FDM (FFF)")])
    assert match_offer(it, o, RULES).checks[0].status == CheckStatus.OK


def test_cartridge_original_vs_compatible():
    from app.engine.matching import apply_card_rules
    it = apply_card_rules(Item(
        id="i1", number=1, name="Картридж HP 59A", brand="HP", model="59A / CF259A", quantity=Decimal(10), unit="шт.",
        requirements=[req("r1", "Тип картриджа", Op.EQ, "лазерный", mandatory=True),
                      req("r2", "Оригинальность", Op.EQ, "оригинальный", mandatory=False),
                      req("r3", "Ресурс", Op.GE, "3000", unit="страниц", mandatory=True)]))
    assert [r.mandatory for r in it.requirements] == [True, True, True]
    assert [r.by_brand for r in it.requirements] == [True, False, True]   # оригинальность моделью не задаётся
    assert match_offer(it, web_offer("Картридж HP CF259A (59A) оригинальный"), RULES).cls == MatchClass.IDENTICAL
    assert match_offer(it, web_offer("Тонер-картридж HP LaserJet Pro CF259A, оригинальный"), RULES).cls == MatchClass.IDENTICAL
    compat = match_offer(it, web_offer("Картридж Sakura CF259A совместимый, для HP"), RULES)
    assert compat.cls == MatchClass.UNSUITABLE and not compat.not_confirmed
    assert match_offer(it, web_offer("Картридж HP 42A Q5942A оригинальный"), RULES).cls == MatchClass.UNSUITABLE


def test_model_code_with_cyrillic_lookalikes():
    """В реестре ЕИС артикул бывает набран с кириллической «С»: «СF-259A», «Оригинальный*СF259A»."""
    assert model_match("59A / CF259A", "Картридж. Оригинальный* СF-259A: HP LaserJet Pro M304a")
    assert model_match("59A / CF259A", "Товарный знак: Оригинальный*СF259A HP")
    assert not model_match("59A / CF259A", "Картридж. Оригинальный* CE320A (№ 128A) Black")


def test_other_model_is_not_homogeneous_when_tz_names_only_the_model():
    """ТЗ одной строкой: модель и оригинальность. Картридж другой модели не взаимозаменяем — не однородный."""
    from app.ingest.reader import read_input
    from app.ingest.rules_parser import parse_procurement
    text = "Картридж HP 59A (CF259A) оригинальный, ресурс 3000 страниц — 4 шт."
    it = parse_procurement(read_input(text.encode(), "описание.txt")).items[0]
    same = match_offer(it, web_offer("Картридж лазерный HP 59А (CF259A) черный, с чипом, оригинальный"), RULES)
    assert same.cls == MatchClass.IDENTICAL
    for title in ("Картридж лазерный HP 83X (CF283X) черный, с чипом, оригинальный",
                  "Картридж. Оригинальный* CE320A (№ 128A) Black: HP Color LaserJet Pro CP1525n"):
        assert match_offer(it, web_offer(title), RULES).cls == MatchClass.UNSUITABLE, title
    kyocera = parse_procurement(read_input("Тонер-картридж Kyocera TK-1170 оригинальный — 6 шт.".encode(),
                                           "описание.txt")).items[0]
    other = match_offer(kyocera, web_offer("Тонер-картридж Kyocera TK-1160 оригинальный"), RULES)
    assert other.cls == MatchClass.UNSUITABLE and "другая модель" in other.reasons[0]
    assert match_offer(kyocera, web_offer("Тонер-картридж Kyocera TK-1170 оригинальный, 7200 стр."), RULES).cls \
        == MatchClass.IDENTICAL
