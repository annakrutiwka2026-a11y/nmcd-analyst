"""Приведение цены предложения к единице закупки: ступень цены, содержимое единицы, тара, НДС, доставка, ИПЦ.

Каждый шаг записывается с коэффициентом и пояснением, чтобы приведённую цену можно было пересчитать вручную.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from app.engine.cpi import CpiSeries, as_date, older_than
from app.engine.rules import Rules
from app.engine.units import canon_unit, convert, dimension, fmt_money, fmt_num, parse_multipack, parse_quantity
from app.models import Item, NormStep, Normalization, Offer, Op, Quantity, SourceKind

_COUNT_LIKE = {"шт", "упаковка", "пачка", "комплект", "набор", "флакон", "уп", "единица"}


def _qty(q: Quantity) -> str:
    return f"{fmt_num(q.value)} {q.unit}"


def _content_factor(item: Item, offer: Offer, rules: Rules, steps: list[NormStep]) -> tuple[Optional[Decimal], Optional[str], list[str]]:
    """Коэффициент перехода от цены источника к цене единицы закупки. None — привести нельзя (с причиной)."""
    flags: list[str] = []
    item_unit = canon_unit(item.unit) or item.unit
    offer_unit = canon_unit(offer.price_unit)
    factor = Decimal(1)
    # тара: цена за коробку (упаковку) из N единиц закупки
    if offer_unit != item_unit:
        mp = parse_multipack(f"{offer.price_unit} {offer.title}")
        if mp is not None and (canon_unit(mp[1]) or mp[1]) == item_unit and mp[0] > 1 and \
                (offer_unit is None or offer_unit in rules.normalize.bulk_units or offer_unit in _COUNT_LIKE):
            factor = Decimal(1) / mp[0]
            steps.append(NormStep(kind="unit", factor=factor,
                                  note=f"цена за {offer.price_unit or 'упаковку'} из {fmt_num(mp[0])} {item_unit}: ÷ {fmt_num(mp[0])}"))
            offer_unit = item_unit
    if item.content is not None:
        need_unit = canon_unit(item.content.unit) or item.content.unit
        content = offer.content
        if content is None and rules.normalize.content_from_title:
            q = parse_quantity(offer.title, prefer=need_unit)
            if q is not None and dimension(canon_unit(q.unit)) == dimension(need_unit):
                content = q
        if content is not None:
            have_unit = canon_unit(content.unit) or content.unit
            have = content.value if have_unit == need_unit else convert(content.value, have_unit, need_unit)
            if have is not None and have > 0:
                k = item.content.value / have
                ratio = rules.normalize.max_content_ratio
                per_measure = dimension(canon_unit(offer.price_unit)) == dimension(need_unit)   # цена за 1 мкг, 1 мл
                if dimension(need_unit) not in ("count", "sheet") and not per_measure and (k > ratio or k < 1 / ratio):
                    return None, (f"фасовка в источнике {_qty(content)}, по ТЗ {fmt_num(item.content.value)} {need_unit}: "
                                  f"при разнице больше чем в {fmt_num(ratio)} раза цена пропорционально не пересчитывается"), flags
                note = (f"цена за {_qty(content)} пересчитана на {fmt_num(item.content.value)} {need_unit}: × {fmt_num(k)}"
                        if k != 1 else f"содержимое единицы совпадает: {_qty(content)}")
                steps.append(NormStep(kind="content", factor=k, note=note))
                return factor * k, None, flags
        if offer_unit == item_unit and item.content_op == Op.EQ and rules.normalize.assume_equal_units:
            flags.append("CONTENT_ASSUMED")
            steps.append(NormStep(kind="unit", factor=Decimal(1),
                                  note=f"цена за {item.unit}; содержимое в источнике не указано, принято как в ТЗ "
                                       f"({_qty(item.content)})"))
            return factor, None, flags
        return None, (f"в источнике не указано содержимое единицы (по ТЗ {'не менее ' if item.content_op == Op.GE else ''}"
                      f"{_qty(item.content)}): цену нельзя привести к единице закупки"), flags
    if offer_unit == item_unit:
        return factor, None, flags
    if offer_unit and item_unit and dimension(offer_unit) and dimension(offer_unit) == dimension(item_unit) \
            and dimension(offer_unit) not in ("container", "count"):
        conv = convert(Decimal(1), item_unit, offer_unit)
        if conv is not None:
            steps.append(NormStep(kind="unit", factor=conv,
                                  note=f"цена за {offer_unit} пересчитана на {item_unit}: × {fmt_num(conv)}"))
            return factor * conv, None, flags
    if offer_unit in rules.normalize.bulk_units:
        return None, f"цена указана за {offer.price_unit} без числа единиц внутри: привести к «{item.unit}» нельзя", flags
    if rules.normalize.assume_equal_units and (offer_unit is None or offer_unit in _COUNT_LIKE) and \
            (item_unit in _COUNT_LIKE or dimension(item_unit) == "count"):
        flags.append("UNIT_ASSUMED")
        steps.append(NormStep(kind="unit", factor=Decimal(1),
                              note=f"единица в источнике «{offer.price_unit}», принята равной единице закупки «{item.unit}»"))
        return factor, None, flags
    return None, f"единица источника «{offer.price_unit}» несопоставима с единицей закупки «{item.unit}»", flags


def normalize_offer(item: Item, offer: Offer, rules: Rules, calc_date: date, cpi: Optional[CpiSeries] = None,
                    delivery_required: Optional[bool] = None) -> Normalization:
    steps: list[NormStep] = []
    flags: list[str] = []
    if offer.price is None or offer.price <= 0:
        return Normalization(offer_id=offer.id, ok=False, problem="в источнике нет цены")
    price = Decimal(offer.price)
    tiers = offer.extra.get("tiers") or []
    if tiers:
        need = item.quantity
        best = None
        for t in tiers:
            try:
                mq, tp = Decimal(str(t.get("min_qty", "1"))), Decimal(str(t.get("price")))
            except ArithmeticError:
                continue
            if mq <= need and (best is None or mq > best[0]):
                best = (mq, tp)
        if best is not None and best[1] != price:
            steps.append(NormStep(kind="tier", factor=best[1] / price,
                                  note=f"ступень цены от {fmt_num(best[0])} {item.unit} для объёма {fmt_num(need)}: {fmt_money(best[1])}"))
            price = best[1]
    factor, problem, cflags = _content_factor(item, offer, rules, steps)
    flags += cflags
    if factor is None:
        return Normalization(offer_id=offer.id, ok=False, problem=problem, steps=steps, flags=flags)
    unit_price = price * factor
    price_day = as_date(offer.price_date or offer.retrieved_at)
    threshold = rules.method.cpi_threshold_months
    old = older_than(price_day, calc_date, threshold)
    vat_text = (offer.vat_rate or "").lower()
    if offer.source in (SourceKind.EIS44, SourceKind.EIS223, SourceKind.MANUAL):   # цена как оплачивается
        if "без" in vat_text:
            flags.append("NON_VAT_PAYER")
    elif offer.price_includes_vat is False:
        rate = rules.vat.rate_at(price_day)
        unit_price *= (1 + rate)
        steps.append(NormStep(kind="vat", factor=1 + rate,
                              note=f"цена без НДС: добавлен НДС {fmt_num(rate * 100)} % (ставка на дату цены)"))
    if not old and price_day < rules.vat.change_date <= calc_date and "без" not in vat_text and \
            offer.price_includes_vat is not False:
        k = (1 + rules.vat.rate_2026) / (1 + rules.vat.rate_before_2026)
        unit_price *= k
        steps.append(NormStep(kind="vat", factor=k, note="цена до 01.01.2026 с НДС 20 %: пересчёт на ставку 22 %"))
    if delivery_required and offer.delivery_included is False:
        if offer.delivery_cost is not None and item.quantity > 0:
            add = Decimal(offer.delivery_cost) / item.quantity
            unit_price += add
            steps.append(NormStep(kind="delivery", factor=Decimal(1),
                                  note=f"доставка {fmt_money(Decimal(offer.delivery_cost))} на заказ: + {fmt_money(add)} на единицу"))
        else:
            flags.append("DELIVERY_NOT_NORMALIZED")
    if old:
        if cpi is None:
            flags.append("CPI_NOT_APPLIED")
            steps.append(NormStep(kind="cpi", factor=Decimal(1), note="ряд ИПЦ не загружен: цена не проиндексирована"))
        else:
            k, why = cpi.factor(price_day, calc_date, threshold, formula=rules.method.cpi_formula,
                                from_price_month=rules.method.cpi_from_price_month,
                                max_lag_months=rules.method.cpi_max_lag_months)
            if k is None:
                flags.append("CPI_NOT_APPLIED")
                steps.append(NormStep(kind="cpi", factor=Decimal(1), note=f"индексация не выполнена: {why}"))
            elif k != 1:
                unit_price *= k
                steps.append(NormStep(kind="cpi", factor=k, note=f"цена от {price_day.strftime('%d.%m.%Y')} приведена к текущему уровню: {why}"))
    return Normalization(offer_id=offer.id, ok=True, unit_price=unit_price, steps=steps, flags=flags)
