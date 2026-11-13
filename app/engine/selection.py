"""Отбор цен и расчёт НМЦД позиции.

Порядок один для всех позиций:
1. правовые фильтры (нет цены, несопоставимая единица, срок давности, неисполненный контракт, неустойки, РНП,
   нет сведений о продавце, несопоставимые характеристики);
2. дубли и повторные цены одного поставщика;
3. набор A — идентичные предложения, проверка на выбросы;
4. если в A меньше нужного числа поставщиков — набор B: идентичные и однородные (режим stepwise);
5. выбросы: модифицированный z-score по медиане и MAD (Iglewicz, Hoaglin) и отношение к медиане;
6. достаточность и однородность: не менее min_suppliers цен разных поставщиков и CV не выше порога —
   тогда НМЦД = количество × цена за единицу (среднее арифметическое, п. 3.21); иначе цена не формируется.
Цены, прошедшие проверку на выброс, ради порога CV не исключаются.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from statistics import median
from typing import Optional
from urllib.parse import urlsplit

from app.engine.cpi import add_months, as_date
from app.engine.rules import Rules
from app.engine.units import fmt_money, fmt_num
from app.models import (CalcResult, CalcStatus, Decision, DecisionCode, Hint, Item, MatchClass, MatchResult,
                        Normalization, Offer, SourceKind)

_CLASS_RU = {MatchClass.IDENTICAL: "идентичное", MatchClass.HOMOGENEOUS: "однородное"}


@dataclass
class _Cand:
    offer: Offer
    match: Optional[MatchResult]
    norm: Optional[Normalization]
    price: Optional[Decimal] = None
    supplier: str = ""
    flags: list[str] = field(default_factory=list)
    forced: bool = False      # включено специалистом


def _pct(x: Decimal) -> str:
    return fmt_num((x * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + " %"


def _site(url: str) -> str:
    """Сайт продавца без поддоменов: shop.helicon.ru → helicon.ru, www.x.msk.ru → x.msk.ru."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in ("com", "net", "org", "msk", "spb", "co", "edu", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _supplier_key(o: Offer) -> tuple[str, list[str]]:
    """Ключ поставщика. Для реестра — ИНН (или наименование). Для сайта продавец — владелец сайта: ключ — ИНН
    со страницы или сам сайт; наименование «ООО …» со страницы для склейки не берётся (часто это производитель)."""
    if o.supplier_inn:
        return "inn:" + o.supplier_inn, []
    if o.source == SourceKind.WEB:
        site = _site(o.url)
        return ("site:" + site, ["SUPPLIER_BY_DOMAIN"]) if site else ("", [])
    if o.supplier_name:
        return "name:" + " ".join(o.supplier_name.lower().split()), []
    return "", []


def _same_supplier_groups(cands: list["_Cand"]) -> list[list["_Cand"]]:
    """Объединяет предложения одного поставщика: общий ИНН или один и тот же сайт."""
    parent = list(range(len(cands)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[str, int] = {}
    for i, c in enumerate(cands):
        keys = {c.supplier}
        if c.offer.source == SourceKind.WEB:
            keys.add("site:" + _site(c.offer.url))
        for k in keys:
            if not k:
                continue
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i
    groups: dict[int, list[_Cand]] = {}
    for i, c in enumerate(cands):
        groups.setdefault(find(i), []).append(c)
    return list(groups.values())


def _ref(o: Offer) -> str:
    """Короткая ссылка на предложение для пояснений: «контракт № … от 21.06.2024», «сайт shop.ru, 12.11.2026»."""
    day = as_date(o.price_date or o.retrieved_at).strftime("%d.%m.%Y")
    if o.contract_number:
        return f"контракт № {o.contract_number} от {day}"
    if o.source == SourceKind.MANUAL:
        return f"коммерческое предложение от {day}"
    return f"{o.source_name or 'сайт'}, {day}"


def _stats(prices: list[Decimal]) -> dict[str, Optional[Decimal]]:
    with localcontext() as ctx:
        ctx.prec = 28
        n = len(prices)
        if n == 0:
            return {"mean": None, "median": None, "min": None, "max": None, "sd": None, "cv": None}
        mean = sum(prices, Decimal(0)) / n
        sd = None
        cv = None
        if n >= 2:
            var = sum(((p - mean) ** 2 for p in prices), Decimal(0)) / (n - 1)
            sd = var.sqrt()
            cv = sd / mean if mean else None
        return {"mean": mean, "median": Decimal(median(prices)), "min": min(prices), "max": max(prices), "sd": sd,
                "cv": cv}


def _outliers(cands: list[_Cand], rules: Rules, decisions: dict[str, Decision]) -> list[_Cand]:
    """Один проход: возвращает оставшиеся цены; исключённые получают решение, подозрительные — флаг."""
    o = rules.outliers
    pool = [c for c in cands if c.price is not None and c.price > 0]
    n = len(pool)
    if n < 3:
        return pool
    with localcontext() as ctx:
        ctx.prec = 28
        med_p = Decimal(median([c.price for c in pool]))
        logs = {c.offer.id: c.price.ln() for c in pool}
        med_x = Decimal(median(list(logs.values())))
        mad = Decimal(median([abs(x - med_x) for x in logs.values()]))
        keep: list[_Cand] = []
        for c in pool:
            ratio = c.price / med_p
            far_flag = ratio > o.ratio_flag or ratio < 1 / o.ratio_flag
            if n >= o.mad_min_n and mad > 0:
                m = Decimal("0.6745") * (logs[c.offer.id] - med_x) / mad
                if abs(m) > o.mad_z and far_flag and not c.forced:
                    decisions[c.offer.id] = Decision(
                        offer_id=c.offer.id, included=False, code=DecisionCode.OUTLIER_MAD, flags=c.flags,
                        reason=(f"цена {fmt_money(c.price)} отличается от медианы {fmt_money(med_p)} в "
                                f"{fmt_num(max(ratio, 1 / ratio).quantize(Decimal('0.01')))} раза; модифицированный "
                                f"z = {fmt_num(m.quantize(Decimal('0.01')))}, порог {fmt_num(o.mad_z)}"))
                    continue
                if abs(m) > o.mad_z:
                    c.flags.append("MAD_ONLY")
                far_excl = ratio > o.ratio_exclude or ratio < 1 / o.ratio_exclude
                if o.ratio_any_n and far_excl and not c.forced:
                    decisions[c.offer.id] = Decision(
                        offer_id=c.offer.id, included=False, code=DecisionCode.OUTLIER_RATIO, flags=c.flags,
                        reason=(f"цена {fmt_money(c.price)} отличается от медианы {fmt_money(med_p)} в "
                                f"{fmt_num(max(ratio, 1 / ratio).quantize(Decimal('0.01')))} раза "
                                f"(больше порога {fmt_num(o.ratio_exclude)})"))
                    continue
            else:
                far_excl = ratio > o.ratio_exclude or ratio < 1 / o.ratio_exclude
                if far_excl and not c.forced:
                    decisions[c.offer.id] = Decision(
                        offer_id=c.offer.id, included=False, code=DecisionCode.OUTLIER_RATIO, flags=c.flags,
                        reason=(f"цена {fmt_money(c.price)} отличается от медианы {fmt_money(med_p)} в "
                                f"{fmt_num(max(ratio, 1 / ratio).quantize(Decimal('0.01')))} раза "
                                f"(порог {fmt_num(o.ratio_exclude)} при малом числе цен)"))
                    continue
                if far_flag:
                    c.flags.append("SUSPICIOUS_RATIO")
            keep.append(c)
    return keep


def _hints(used: list[_Cand]) -> list[Hint]:
    if len(used) < 3:
        return []
    prices = [c.price for c in used]
    med = Decimal(median(prices))
    mean = sum(prices, Decimal(0)) / len(prices)
    order = sorted(used, key=lambda c: (-abs(c.price.ln() - med.ln()), -abs(c.price - mean), c.offer.id))
    hints = []
    for c in order:
        rest = [x.price for x in used if x is not c]
        st = _stats(rest)
        if st["cv"] is not None:
            hints.append(Hint(without_offer_id=c.offer.id, cv=st["cv"], mean=st["mean"]))
    return hints


def decide_and_calculate(item: Item, offers: list[Offer], matches: list[MatchResult], norms: list[Normalization],
                         rules: Rules, calc_date: date,
                         overrides: Optional[dict[str, tuple[bool, str]]] = None) -> tuple[list[Decision], CalcResult]:
    overrides = overrides or {}
    by_match = {m.offer_id: m for m in matches}
    by_norm = {n.offer_id: n for n in norms}
    decisions: dict[str, Decision] = {}
    cands: list[_Cand] = []
    cutoff = add_months(calc_date, -rules.method.max_age_months)
    executed = {s.lower() for s in rules.method.executed_statuses}

    for o in sorted(offers, key=lambda x: x.id):
        m, n = by_match.get(o.id), by_norm.get(o.id)
        c = _Cand(offer=o, match=m, norm=n)
        c.flags = list(n.flags if n else [])
        ov = overrides.get(o.id)

        def drop(code: DecisionCode, reason: str) -> None:
            decisions[o.id] = Decision(offer_id=o.id, included=False, code=code, reason=reason, flags=c.flags)

        forced = ov is not None and ov[0]
        # сначала — сопоставимость: «другой товар» объясняет исключение лучше, чем отсутствие цены или фасовки
        if not forced and (m is None or m.cls == MatchClass.UNSUITABLE):
            reason = "; ".join(m.reasons) if m and m.reasons else "характеристики не совпадают с ТЗ"
            drop(DecisionCode.NOT_CONFIRMED if m and m.not_confirmed else DecisionCode.UNSUITABLE, reason)
            continue
        if o.price is None or o.price <= 0:
            drop(DecisionCode.NO_PRICE, "в источнике нет цены")
            continue
        if n is None or not n.ok or n.unit_price is None:
            drop(DecisionCode.NO_UNIT, (n.problem if n else None) or "цену не удалось привести к единице закупки")
            continue
        c.price = n.unit_price
        c.supplier, extra_flags = _supplier_key(o)
        c.flags += extra_flags
        if o.extra.get("legal_checked"):
            if o.source == SourceKind.EIS44 and o.contract_number and o.penalties is None:
                c.flags.append("PENALTIES_UNKNOWN")
            if o.supplier_inn and o.extra.get("rnp") is None:
                c.flags.append("RNP_UNKNOWN")
        if ov is not None:
            include, why = ov
            if not include:
                decisions[o.id] = Decision(offer_id=o.id, included=False, code=DecisionCode.USER_EXCLUDED,
                                           reason=f"исключено специалистом: {why}", flags=c.flags, actor="user")
                continue
            c.forced = True
            cands.append(c)
            continue
        price_day = as_date(o.price_date or o.retrieved_at)
        if o.source in (SourceKind.EIS44, SourceKind.EIS223, SourceKind.MANUAL) and price_day < cutoff:
            drop(DecisionCode.TOO_OLD, f"цена от {price_day.strftime('%d.%m.%Y')} старше {rules.method.max_age_months} мес.")
            continue
        if o.source == SourceKind.EIS44 and (o.contract_status or "").strip().lower() not in executed:
            drop(DecisionCode.NOT_EXECUTED, f"контракт не исполнен (статус: {o.contract_status or 'не указан'})")
            continue
        if o.penalties:
            drop(DecisionCode.PENALTIES, "по контракту взыскивались неустойки")
            continue
        if o.extra.get("rnp"):
            drop(DecisionCode.RNP, "поставщик включён в реестр недобросовестных поставщиков")
            continue
        if not c.supplier:
            drop(DecisionCode.ANONYMOUS, "нет сведений о продавце (ни ИНН, ни наименования)")
            continue
        cands.append(c)

    # дубли: та же страница или позиция контракта, затем повтор поставщика
    seen_keys: dict[str, _Cand] = {}
    unique: list[_Cand] = []
    for c in cands:
        # одна страница или позиция контракта — один источник; у КП адреса нет, каждое КП — отдельный источник
        key = f"{c.offer.url}#{c.offer.extra.get('position_index', '')}" if c.offer.url else f"id:{c.offer.id}"
        if key in seen_keys and not c.forced:
            decisions[c.offer.id] = Decision(offer_id=c.offer.id, included=False, code=DecisionCode.DUPLICATE,
                                             reason=f"дубль: та же страница или позиция, что и {_ref(seen_keys[key].offer)}",
                                             flags=c.flags)
            continue
        seen_keys[key] = c
        unique.append(c)
    kept: list[_Cand] = []
    for group in _same_supplier_groups(unique):
        group.sort(key=lambda c: (not c.forced, -as_date(c.offer.price_date or c.offer.retrieved_at).toordinal(),
                                  c.price, c.offer.id))
        kept.append(group[0])
        for c in group[1:]:
            if c.forced:
                kept.append(c)
                continue
            decisions[c.offer.id] = Decision(
                offer_id=c.offer.id, included=False, code=DecisionCode.DUP_SUPPLIER, flags=c.flags,
                reason=(f"повторная цена того же поставщика: в расчёте одна его цена, самая поздняя (при равных датах — "
                        f"меньшая): {_ref(group[0].offer)}"))
    kept.sort(key=lambda c: c.offer.id)

    # цены, возвращённые специалистом, входят в расчёт всегда, но на выбор набора (A или B) не влияют
    forced = [c for c in kept if c.forced]
    identical = [c for c in kept if not c.forced and c.match and c.match.cls == MatchClass.IDENTICAL]
    homogeneous = [c for c in kept if not c.forced and c.match and c.match.cls == MatchClass.HOMOGENEOUS]
    need = rules.method.min_suppliers

    trial: dict[str, Decision] = {}
    used = _outliers(identical + forced, rules, trial)
    set_used = "A"
    enough_identical = len({c.supplier for c in used if not c.forced}) >= need
    if enough_identical or rules.method.selection == "strict" or not homogeneous:
        decisions.update(trial)
        for c in homogeneous:
            decisions[c.offer.id] = Decision(
                offer_id=c.offer.id, included=False, code=DecisionCode.HOMOGENEOUS_EXCLUDED, flags=c.flags,
                reason=("идентичных предложений достаточно, однородные в расчёт не берутся"
                        if enough_identical else
                        "режим strict: в расчёт идут только идентичные предложения"))
    else:
        trial = {}
        used = _outliers(identical + homogeneous + forced, rules, trial)
        decisions.update(trial)
        set_used = "B"

    prices = [c.price for c in used]
    st = _stats(prices)
    n_sup = len({c.supplier for c in used})
    cv = st["cv"]
    final = n_sup >= need and cv is not None and cv <= rules.method.cv_threshold
    q = rules.rounding
    unit_price = total = None
    if final:
        base = {"mean": st["mean"], "median": st["median"], "min": st["min"]}[rules.method.formula]
        unit_price = base.quantize(q, rounding=ROUND_HALF_UP)
        total = (unit_price * item.quantity).quantize(q, rounding=ROUND_HALF_UP)
        words = {2: "двух", 3: "трёх", 4: "четырёх", 5: "пяти"}
        note_b = f"; идентичных меньше {words.get(need, need)}, использованы однородные" if set_used == "B" else ""
        message = (f"Цена обоснована: цены {n_sup} разных поставщиков, коэффициент вариации {_pct(cv)} "
                   f"не выше {_pct(rules.method.cv_threshold)}{note_b}.")
    else:
        if n_sup < need:
            message = (f"Цена не сформирована: цен разных поставщиков найдено {n_sup}, нужно не менее {need} "
                       f"(п. 3.19). Нужны дополнительные цены — запрос коммерческих предложений.")
        else:
            message = (f"Цена не сформирована: коэффициент вариации {_pct(cv)} выше порога "
                       f"{_pct(rules.method.cv_threshold)} (п. 3.20) — набор цен неоднороден, нужны дополнительные цены.")
    for c in used:
        cls = _CLASS_RU.get(c.match.cls, "") if c.match else ""
        if c.forced:
            why = overrides.get(c.offer.id, (True, ""))[1]
            decisions[c.offer.id] = Decision(offer_id=c.offer.id, included=True, code=DecisionCode.USER_INCLUDED,
                                             reason=f"включено специалистом: {why}", flags=c.flags, actor="user")
            continue
        reason = f"{cls}: {'; '.join(c.match.reasons[:2]) if c.match else ''}".strip(": ")
        if "SUSPICIOUS_RATIO" in c.flags or "MAD_ONLY" in c.flags:
            reason += "; цена заметно отличается от медианы, проверьте вручную"
        decisions[c.offer.id] = Decision(offer_id=c.offer.id, included=True, code=DecisionCode.INCLUDED,
                                         reason=reason, flags=c.flags)
    ordered = [decisions[o.id] for o in sorted(offers, key=lambda x: x.id) if o.id in decisions]
    calc = CalcResult(
        status=CalcStatus.FINAL if final else CalcStatus.INSUFFICIENT, set_used=set_used, n=len(used),
        n_suppliers=n_sup, prices=sorted(prices), offer_ids=[c.offer.id for c in sorted(used, key=lambda c: c.price)],
        mean=st["mean"], median=st["median"], min=st["min"], max=st["max"], sd=st["sd"], cv=cv,
        unit_price=unit_price, total=total, message=message, hints=[] if final else _hints(used),
    )
    return ordered, calc
