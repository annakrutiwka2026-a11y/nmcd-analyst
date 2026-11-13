"""Ядро: контрольные примеры C1–C11 (посчитаны независимым скриптом) и случаи из трёх ТЗ кейса."""
from datetime import date, datetime, timezone
from decimal import Decimal

from app.engine.cpi import CpiSeries
from app.engine.matching import match_offer
from app.engine.normalize import normalize_offer
from app.engine.rules import load_rules
from app.engine.selection import decide_and_calculate
from app.models import (CalcStatus, CheckStatus, DecisionCode, Item, MatchClass, MatchResult, Normalization, Offer,
                        OfferChar, Op, Quantity, Requirement, SourceKind)

RULES = load_rules()
CALC = date(2026, 11, 12)
T0 = datetime(2026, 11, 10, tzinfo=timezone.utc)


def item(**kw) -> Item:
    base = dict(id="i1", number=1, name="Товар", quantity=Decimal(1), unit="шт.")
    base.update(kw)
    return Item(**base)


def offer(oid: str, price, **kw) -> Offer:
    base = dict(id=oid, item_id="i1", source=SourceKind.WEB, source_name="shop.ru", url=f"https://s{oid}.ru/p",
                retrieved_at=T0, title="Товар", supplier_name=f"ООО «{oid}»", supplier_inn=None,
                price=Decimal(str(price)), price_unit="шт.", price_date=T0)
    base.update(kw)
    return Offer(**base)


def run(prices, qty=1, classes=None):
    it = item(quantity=Decimal(qty))
    offers = [offer(f"o{k}", p) for k, p in enumerate(prices, start=1)]
    matches = [MatchResult(offer_id=o.id, cls=(classes or {}).get(o.id, MatchClass.IDENTICAL), score=1.0) for o in offers]
    norms = [Normalization(offer_id=o.id, ok=True, unit_price=o.price) for o in offers]
    decisions, calc = decide_and_calculate(it, offers, matches, norms, RULES, CALC)
    return {d.offer_id: d for d in decisions}, calc


def q(x, places="0.0001"):
    return Decimal(x).quantize(Decimal(places))


def test_c1_mad_catches_outlier_cv_misses():
    d, c = run([78500, 81200, 79900, 83000, 80400, 129000], qty=12)
    assert d["o6"].code == DecisionCode.OUTLIER_MAD
    assert c.status == CalcStatus.FINAL and c.n == 5
    assert c.mean == Decimal("80600") and q(c.sd) == Decimal("1662.8289") and q(c.cv * 100) == Decimal("2.0631")
    assert c.total == Decimal("967200.00")


def test_c2_small_n():
    d, c = run([100, 100, 130], qty=10)
    # у сайтов без ИНН есть только справочная пометка «поставщик определён по сайту»
    assert all(x.included for x in d.values())
    assert not any(f for x in d.values() for f in x.flags if f != "SUPPLIER_BY_DOMAIN")
    assert c.mean == Decimal("110") and q(c.sd) == Decimal("17.3205") and q(c.cv * 100) == Decimal("15.7459")
    assert c.total == Decimal("1100.00")


def test_c3_cv_above_threshold_no_auto_trim():
    d, c = run([40000, 52000, 85000, 50000], qty=4)
    assert all(x.included for x in d.values())
    assert "SUSPICIOUS_RATIO" in d["o3"].flags
    assert c.status == CalcStatus.INSUFFICIENT and c.total is None and q(c.cv * 100) == Decimal("34.4515")
    assert c.mean == Decimal("56750")
    assert c.hints[0].without_offer_id == "o3" and q(c.hints[0].cv * 100) == Decimal("13.5826")
    assert c.hints[0].mean.quantize(Decimal("0.01")) == Decimal("47333.33")


def test_c6_mad_zero_uses_ratio_rule():
    d, c = run([100, 100, 100, 104, 250], qty=10)
    assert d["o5"].code == DecisionCode.OUTLIER_RATIO
    assert c.mean == Decimal("101") and q(c.cv * 100) == Decimal("1.9802") and c.total == Decimal("1010.00")


def test_c7_too_few_after_outliers():
    d, c = run([10000, 50000, 52000, 120000])
    assert d["o1"].code == d["o4"].code == DecisionCode.OUTLIER_RATIO
    assert c.status == CalcStatus.INSUFFICIENT and c.n_suppliers == 2


def test_c8_bounds_and_tie_break():
    d, c = run([50, 100, 200, 100])
    assert all(x.included for x in d.values())
    assert "SUSPICIOUS_RATIO" in d["o1"].flags and "SUSPICIOUS_RATIO" in d["o3"].flags
    assert q(c.cv * 100) == Decimal("55.9247") and c.status == CalcStatus.INSUFFICIENT
    assert c.hints[0].without_offer_id == "o3"


def test_c11_dense_set_mad_only_flag():
    d, c = run([100, 101, 102, 103, 115])
    assert d["o5"].included and "MAD_ONLY" in d["o5"].flags
    assert c.mean == Decimal("104.2") and q(c.cv * 100) == Decimal("5.8925") and c.status == CalcStatus.FINAL


def test_order_independence_and_invariant():
    prices = [313.46, 319, 334, 337, 295.47, 340]
    _, c1 = run(prices, qty=2500)
    _, c2 = run(list(reversed(prices)), qty=2500)
    assert c1.total == c2.total and c1.cv == c2.cv
    assert c1.min * 2500 - Decimal("0.005") <= c1.total <= c1.max * 2500 + Decimal("0.005")


def test_stepwise_uses_homogeneous_when_identical_short():
    cls = {"o1": MatchClass.IDENTICAL, "o2": MatchClass.HOMOGENEOUS, "o3": MatchClass.HOMOGENEOUS}
    d, c = run([100, 102, 104], classes=cls)
    assert c.set_used == "B" and c.status == CalcStatus.FINAL and "однородные" in c.message


def test_identical_enough_excludes_homogeneous():
    cls = {"o4": MatchClass.HOMOGENEOUS}
    d, c = run([100, 102, 104, 150], classes=cls)
    assert d["o4"].code == DecisionCode.HOMOGENEOUS_EXCLUDED and c.n == 3


def test_overrides():
    it = item(quantity=Decimal(1))
    offers = [offer(f"o{k}", p) for k, p in enumerate([100, 100, 100, 104, 250], start=1)]
    matches = [MatchResult(offer_id=o.id, cls=MatchClass.IDENTICAL, score=1.0) for o in offers]
    norms = [Normalization(offer_id=o.id, ok=True, unit_price=o.price) for o in offers]
    d, c = decide_and_calculate(it, offers, matches, norms, RULES, CALC,
                                {"o5": (True, "редкая комплектация, цена подтверждена письмом"), "o1": (False, "дубль")})
    dd = {x.offer_id: x for x in d}
    assert dd["o5"].code == DecisionCode.USER_INCLUDED and dd["o1"].code == DecisionCode.USER_EXCLUDED


def test_duplicate_supplier_keeps_latest():
    it = item()
    old = offer("o1", 100, supplier_inn="7720945802", price_date=datetime(2026, 5, 1, tzinfo=timezone.utc))
    new = offer("o2", 110, supplier_inn="7720945802")
    others = [offer("o3", 105), offer("o4", 108)]
    offers = [old, new] + others
    matches = [MatchResult(offer_id=o.id, cls=MatchClass.IDENTICAL, score=1.0) for o in offers]
    norms = [Normalization(offer_id=o.id, ok=True, unit_price=o.price) for o in offers]
    d, c = decide_and_calculate(it, offers, matches, norms, RULES, CALC)
    dd = {x.offer_id: x for x in d}
    assert dd["o1"].code == DecisionCode.DUP_SUPPLIER and dd["o2"].included


# ------------------------------------------------------------------ приведение


def test_c4_cpi_chain_factor():
    cpi = CpiSeries([("2026-01", "100.50", "2026-02-15"), ("2026-02", "100.30", "2026-03-15"),
                     ("2026-03", "100.80", "2026-04-15")])
    k, why = cpi.factor(date(2025, 12, 20), date(2026, 6, 25), 0)
    assert k == Decimal("1.016079120")
    assert (Decimal("50000") * k).quantize(Decimal("0.01")) == Decimal("50803.96")


def test_c5_vat_added():
    it = item()
    o = offer("o1", "10000.00", price_includes_vat=False)
    n = normalize_offer(it, o, RULES, CALC)
    assert n.unit_price.quantize(Decimal("0.01")) == Decimal("12200.00")


def test_c9_vat_and_cpi_not_double():
    rows = [(f"2025-{m:02d}", "100", None) for m in range(7, 13)] + [("2026-01", "103", "2026-02-10"),
                                                                     ("2026-02", "100", "2026-03-10")]
    cpi = CpiSeries(rows)
    it = item()
    o = offer("o1", "10000.00", price_includes_vat=False, price_date=datetime(2025, 6, 10, tzinfo=timezone.utc))
    n = normalize_offer(it, o, RULES, date(2026, 3, 20), cpi)
    assert n.unit_price.quantize(Decimal("0.01")) == Decimal("12360.00")


def test_c10_tier_and_delivery():
    it = item(quantity=Decimal(20))
    o = offer("o1", "1200.00", delivery_included=False, delivery_cost=Decimal("500"),
              extra={"tiers": [{"min_qty": "1", "price": "1200.00"}, {"min_qty": "10", "price": "1100.00"},
                               {"min_qty": "50", "price": "990.00"}]})
    n = normalize_offer(it, o, RULES, CALC, delivery_required=True)
    assert n.unit_price.quantize(Decimal("0.01")) == Decimal("1125.00")


def test_cytokine_mass_normalization():
    it = item(unit="шт.", content=Quantity(value=Decimal(10), unit="мкг"), content_op=Op.GE)
    o = offer("o1", "12000", content=Quantity(value=Decimal(20), unit="мкг"), price_unit="флакон")
    n = normalize_offer(it, o, RULES, CALC)
    assert n.ok and n.unit_price == Decimal("6000")
    # фасовка в 5 раз больше: цена реактива от массы нелинейна, пропорциональный пересчёт не делается
    o5 = offer("o5", "12000", content=Quantity(value=Decimal(50), unit="мкг"), price_unit="флакон")
    n5 = normalize_offer(it, o5, RULES, CALC)
    assert not n5.ok and "фасовка" in n5.problem
    o2 = offer("o2", "12000", price_unit="флакон")
    n2 = normalize_offer(it, o2, RULES, CALC)
    assert not n2.ok and "содержимое" in n2.problem


def test_eis_price_per_microgram():
    it = item(unit="шт.", content=Quantity(value=Decimal(100), unit="мкг"), content_op=Op.GE)
    o = offer("o1", "150", price_unit="Микрограмм", content=Quantity(value=Decimal(1), unit="мкг"),
              source=SourceKind.EIS44)
    n = normalize_offer(it, o, RULES, CALC)
    assert n.unit_price == Decimal("15000")


def test_box_of_packs():
    it = item(unit="пачка", content=Quantity(value=Decimal(500), unit="лист"))
    o = offer("o1", "1750", price_unit="коробка", title="Бумага А4 SvetoCopy, коробка 5 пачек по 500 л.")
    n = normalize_offer(it, o, RULES, CALC)
    assert n.ok and n.unit_price == Decimal("350")


def test_cpi_missing_flag():
    it = item()
    o = offer("o1", "100", source=SourceKind.EIS44, price_date=datetime(2025, 1, 10, tzinfo=timezone.utc))
    n = normalize_offer(it, o, RULES, CALC, cpi=None)
    assert "CPI_NOT_APPLIED" in n.flags and n.unit_price == Decimal("100")


# ------------------------------------------------------------------ сверка


def req(rid, name, op, value=None, **kw) -> Requirement:
    return Requirement(id=rid, name=name, op=op, value=value, **kw)


PAPER = item(name="Бумага для офисной техники А4 SvetoCopy", brand="SvetoCopy", unit="пачка",
             requirements=[req("r1", "Формат", Op.CONTAINS, "А4", mandatory=True),
                           req("r2", "Масса листа", Op.EQ, "80", unit="г/м²", tolerance="±3", mandatory=True),
                           req("r3", "Белизна CIE", Op.EQ, "146", unit="%", tolerance="±3%", mandatory=False)])


def chars(**kv):
    return [OfferChar(requirement_id=k, name=k, value=v, quote=v, quote_verified=True) for k, v in kv.items()]


def test_paper_identical_latin_a4():
    o = offer("o1", 300, title="Бумага SvetoCopy A4", characteristics=chars(r1="A4", r2="80 г/м2"))
    m = match_offer(PAPER, o, RULES)
    assert m.cls == MatchClass.IDENTICAL


def test_paper_other_brand_is_homogeneous():
    o = offer("o1", 300, title="Бумага Снегурочка А4", characteristics=chars(r1="А4", r2="80"))
    m = match_offer(PAPER, o, RULES)
    assert m.cls == MatchClass.HOMOGENEOUS and "товарный знак" in m.reasons[0]


def test_paper_wrong_density_unsuitable():
    o = offer("o1", 300, title="Бумага SvetoCopy А4", characteristics=chars(r1="А4", r2="65 г/м2"))
    m = match_offer(PAPER, o, RULES)
    assert m.cls == MatchClass.UNSUITABLE and not m.not_confirmed


def test_missing_mandatory_not_confirmed():
    o = offer("o1", 300, title="Бумага SvetoCopy А4", characteristics=chars(r1="А4"))
    m = match_offer(PAPER, o, RULES)
    assert m.cls == MatchClass.UNSUITABLE and m.not_confirmed


def test_llm_verdict_fills_missing_text():
    it = item(name="Интерлейкин-2", requirements=[req("c1", "Клеточная линия", Op.CONTAINS, "CHO", mandatory=True)])
    o = offer("o1", 100, title="rhIL-2", text="Recombinant human IL-2")
    assert match_offer(it, o, RULES).not_confirmed
    m = match_offer(it, o, RULES, {"c1": "ok"})
    assert m.cls == MatchClass.IDENTICAL and m.llm_used


def test_cho_vs_ecoli_fail():
    it = item(name="Интерлейкин-2", requirements=[req("c1", "Клеточная линия", Op.CONTAINS, "CHO", mandatory=True)])
    o = offer("o1", 100, characteristics=chars(c1="E. coli"))
    assert match_offer(it, o, RULES).cls == MatchClass.UNSUITABLE


def test_ge_with_delta_above():
    it = item(requirements=[req("m", "ОЗУ", Op.GE, "16", unit="ГБ", mandatory=True)])
    assert match_offer(it, offer("a", 1, characteristics=chars(m="16 ГБ")), RULES).cls == MatchClass.IDENTICAL
    above = match_offer(it, offer("b", 1, characteristics=chars(m="32 ГБ")), RULES)
    assert above.cls == MatchClass.HOMOGENEOUS and above.checks[0].status == CheckStatus.ABOVE
    assert match_offer(it, offer("c", 1, characteristics=chars(m="8 ГБ")), RULES).cls == MatchClass.UNSUITABLE


def test_printer_model():
    it = item(name="3D принтер CREATBOT D600 PRO", brand="CREATBOT", model="D600 PRO",
              requirements=[req("t", "Технология печати", Op.CONTAINS, "FDM", mandatory=True)])
    pro = offer("a", 1, title="3D принтер CreatBot D600 Pro", characteristics=chars(t="FDM"))
    base = offer("b", 1, title="3D принтер CreatBot D600", characteristics=chars(t="FDM"))
    assert match_offer(it, pro, RULES).cls == MatchClass.IDENTICAL
    assert match_offer(it, base, RULES).cls == MatchClass.HOMOGENEOUS


def test_other_product_reason_comes_first():
    # предложение о другом товаре без цены исключается как «другой товар», а не «нет цены»
    it = item(requirements=[req("r1", "Вид белка", Op.EQ, "Интерлейкин-2", mandatory=True)])
    o = offer("o1", "0")
    o.extra["relevant"] = False
    m = MatchResult(offer_id="o1", cls=MatchClass.UNSUITABLE, reasons=["другой товар: лекарственный препарат"])
    n = normalize_offer(it, o, RULES, CALC)
    decisions, _ = decide_and_calculate(it, [o], [m], [n], RULES, CALC)
    assert decisions[0].code == DecisionCode.UNSUITABLE


def test_numbers_from_model_answer():
    from app.pipeline import _num
    assert _num("1 200,50") == Decimal("1200.50") and _num(12000) == Decimal("12000")
    assert _num("10 мкг") is None and _num(None) is None and _num(True) is None and _num(0) is None


def test_same_seller_on_two_pages_counts_once():
    # две страницы одного сайта — один поставщик; одинаковое «ООО …» на разных сайтах — разные продавцы
    it = item()
    offers = [offer("o1", 100, url="https://shop.helicon.ru/a", supplier_name="ООО «СайСторЛаб»"),
              offer("o2", 110, url="https://www.helicon.ru/b", supplier_name="www.helicon.ru"),
              offer("o3", 105, url="https://biomed.ru/c", supplier_name="ООО «СайСторЛаб»"),
              offer("o4", 108, url="https://dia-m.ru/d", supplier_name="dia-m.ru")]
    matches = [MatchResult(offer_id=o.id, cls=MatchClass.IDENTICAL, score=1.0) for o in offers]
    norms = [Normalization(offer_id=o.id, ok=True, unit_price=o.price) for o in offers]
    decisions, calc = decide_and_calculate(it, offers, matches, norms, RULES, CALC)
    codes = {d.offer_id: d.code for d in decisions}
    assert [codes["o1"], codes["o2"]].count(DecisionCode.DUP_SUPPLIER) == 1
    assert codes["o3"] == DecisionCode.INCLUDED and calc.n == 3


def test_double_median_excluded_at_any_n():
    # 8 цен с широким разбросом: z-score 28 500 ₽ ниже 3,5, но цена в 2,4 раза выше медианы — исключается
    d, c = run([15479, 8829, 14062, 10937, 9522, 13200, 28500, 10700])
    assert d["o7"].code == DecisionCode.OUTLIER_RATIO and "больше порога" in d["o7"].reason
    assert c.status == CalcStatus.FINAL and c.n == 7


def test_returned_offer_does_not_switch_set():
    # 2 идентичных и 4 однородных: набор B; возвращённая специалистом цена не делает набор A
    classes = {"o3": MatchClass.HOMOGENEOUS, "o4": MatchClass.HOMOGENEOUS, "o5": MatchClass.HOMOGENEOUS,
               "o6": MatchClass.HOMOGENEOUS}
    _, base = run([100, 102, 104, 98, 101, 99], classes=classes)
    it = item()
    offers = [offer(f"o{k}", p) for k, p in enumerate([100, 102, 104, 98, 101, 99], start=1)]
    matches = [MatchResult(offer_id=o.id, cls=classes.get(o.id, MatchClass.IDENTICAL), score=1.0) for o in offers]
    norms = [Normalization(offer_id=o.id, ok=True, unit_price=o.price) for o in offers]
    _, forced = decide_and_calculate(it, offers, matches, norms, RULES, CALC, {"o3": (True, "проверено")})
    assert base.set_used == "B" and base.n == 6
    assert forced.set_used == "B" and forced.n == 6 and forced.mean == base.mean


def test_penalties_and_dishonest_supplier_are_excluded():
    """Цена контракта с неустойками и цена поставщика из РНП не идут в расчёт (ч. 18 ст. 22 44-ФЗ, п. 3.13);
    если проверить не удалось, цена остаётся с пометкой."""
    it = item(quantity=Decimal(5))
    eis = dict(source=SourceKind.EIS44, contract_status="Исполнение завершено")
    offers = [offer("o1", 100, **eis, contract_number="1", penalties=True, extra={"legal_checked": True}),
              offer("o2", 101, supplier_inn="7707083893", extra={"legal_checked": True, "rnp": True}),
              offer("o3", 102, **eis, contract_number="3", penalties=None, extra={"legal_checked": True}),
              offer("o4", 103, **eis, contract_number="4", penalties=False, extra={"legal_checked": True}),
              offer("o5", 104), offer("o6", 99)]
    matches = [MatchResult(offer_id=o.id, cls=MatchClass.IDENTICAL, score=1.0) for o in offers]
    norms = [Normalization(offer_id=o.id, ok=True, unit_price=o.price) for o in offers]
    decisions, calc = decide_and_calculate(it, offers, matches, norms, RULES, CALC)
    d = {x.offer_id: x for x in decisions}
    assert d["o1"].code == DecisionCode.PENALTIES and d["o2"].code == DecisionCode.RNP
    assert d["o3"].included and "PENALTIES_UNKNOWN" in d["o3"].flags
    assert d["o4"].included and "PENALTIES_UNKNOWN" not in d["o4"].flags
    assert calc.status == CalcStatus.FINAL and calc.n == 4


def test_legal_checks_only_for_offers_that_passed_matching():
    """Конвейер проверяет неустойки и РНП только у цен, прошедших сверку, и пересчитывает позицию, если нашлось
    основание исключить цену."""
    import asyncio
    from app.config import get_settings
    from app.pipeline import Run, evaluate_item

    class FakeEis:
        def __init__(self):
            self.asked = []

        async def penalties(self, reestr):
            self.asked.append(("p", reestr))
            return reestr == "2", f"https://zakupki.gov.ru/p?{reestr}"

        async def in_rnp(self, inn):
            self.asked.append(("r", inn))
            return False, f"https://zakupki.gov.ru/r?{inn}"

    it = item(quantity=Decimal(2))
    eis = dict(source=SourceKind.EIS44, contract_status="Исполнение завершено")
    offers = [offer("o1", 100, **eis, contract_number="1", supplier_inn="7707083893"),
              offer("o2", 101, **eis, contract_number="2", supplier_inn="7736050003"),
              offer("o3", 102, **eis, contract_number="3", extra={"relevant": False})]
    result = evaluate_item(it, offers, RULES, CALC, None, None)
    run = Run(get_settings(), use_llm=False, use_web=False)
    fake = FakeEis()
    assert asyncio.run(run.check_legal(it, result, fake)) is True
    assert ("p", "3") not in fake.asked and ("p", "1") in fake.asked and ("r", "7707083893") in fake.asked
    again = evaluate_item(it, offers, RULES, CALC, None, None)
    d = {x.offer_id: x for x in again.decisions}
    assert d["o2"].code == DecisionCode.PENALTIES and d["o1"].included
