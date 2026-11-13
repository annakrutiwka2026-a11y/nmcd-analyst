"""Представление отчёта для шаблонов: строки предложений, расчёт по позициям, сводка."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.engine.matching import condition_text
from app.engine.units import canon_unit, dimension
from app.models import CalcStatus, CheckStatus, DecisionCode, ItemResult, MatchClass, Report, SourceKind
from app.report.fmt import _plural, dt, money, num, pct

CLASS_RU = {MatchClass.IDENTICAL: "идентичное", MatchClass.HOMOGENEOUS: "однородное",
            MatchClass.UNSUITABLE: "неподходящее"}
CHECK_RU = {CheckStatus.OK: "совпадает", CheckStatus.FAIL: "не совпадает", CheckStatus.MISSING: "нет данных",
            CheckStatus.ABOVE: "выше требования", CheckStatus.UNCLEAR: "неясно"}
FLAG_RU = {
    "NON_VAT_PAYER": "поставщик без НДС: цена контракта как оплачена",
    "CONTENT_ASSUMED": "содержимое единицы в источнике не указано, принято как в ТЗ",
    "UNIT_ASSUMED": "единица источника принята равной единице закупки",
    "DELIVERY_NOT_NORMALIZED": "стоимость доставки в источнике не указана",
    "CPI_NOT_APPLIED": "индексация по ИПЦ не выполнена",
    "SUPPLIER_BY_DOMAIN": "продавец определён по сайту",
    "MAD_ONLY": "далеко от медианы по z-score, но меньше чем в 1,5 раза",
    "SUSPICIOUS_RATIO": "отличается от медианы больше чем в 1,5 раза",
    "PENALTIES_UNKNOWN": "неустойки по контракту проверить не удалось: ЕИС не ответил",
    "RNP_UNKNOWN": "запись поставщика в РНП проверить не удалось: ЕИС не ответил",
}

CODE_RU = {
    DecisionCode.INCLUDED: "учтено", DecisionCode.NO_PRICE: "нет цены", DecisionCode.NO_UNIT: "единица несопоставима",
    DecisionCode.NO_DATE: "нет даты", DecisionCode.TOO_OLD: "старше трёх лет", DecisionCode.NOT_EXECUTED: "контракт не исполнен",
    DecisionCode.PENALTIES: "были неустойки", DecisionCode.RNP: "поставщик в РНП", DecisionCode.ANONYMOUS: "нет сведений о продавце",
    DecisionCode.UNSUITABLE: "несопоставимые характеристики", DecisionCode.NOT_CONFIRMED: "характеристики не подтверждены",
    DecisionCode.HOMOGENEOUS_EXCLUDED: "однородное (идентичных достаточно)", DecisionCode.DUPLICATE: "дубль",
    DecisionCode.DUP_SUPPLIER: "повтор поставщика", DecisionCode.OUTLIER_MAD: "выброс (MAD)",
    DecisionCode.OUTLIER_RATIO: "выброс (отношение к медиане)", DecisionCode.USER_EXCLUDED: "исключено специалистом",
    DecisionCode.USER_INCLUDED: "включено специалистом",
}
SOURCE_RU = {SourceKind.EIS44: "ЕИС 44-ФЗ", SourceKind.EIS223: "ЕИС 223-ФЗ", SourceKind.WEB: "сайт поставщика",
             SourceKind.MANUAL: "коммерческое предложение"}


_ORG_FORMS = [("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", "ООО"), ("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ПАО"),
              ("НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"), ("ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ЗАО"),
              ("ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ОАО"), ("АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"),
              ("ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ", "ИП")]


def supplier_label(name: str | None) -> str:
    """Короткое наименование поставщика: «ООО "Ландора"» вместо полного наименования прописными буквами."""
    s = " ".join((name or "").split())
    if not s:
        return "—"
    m = re.search(r"\(((?:ООО|АО|ПАО|ЗАО|ОАО|ИП)\s[^()]{2,80})\)\s*$", s)
    if m:
        s = m.group(1)
    up = s.upper()
    for full, short in _ORG_FORMS:
        if up.startswith(full):
            s = short + s[len(full):]
            break
    letters = [ch for ch in s if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.8:
        form, _, rest = s.partition(" ")
        if form in {short for _, short in _ORG_FORMS}:
            s = form + " " + _title_quoted(rest)
        elif re.fullmatch(r"[А-ЯЁ]+(?: [А-ЯЁ]+){1,2}", s):
            s = " ".join(w.capitalize() for w in s.split())          # ФИО индивидуального предпринимателя
    return re.sub(r'"([^"]+)"', "«\\1»", s)


def _title_quoted(rest: str) -> str:
    """«"ЛАНДОРА"» → «"Ландора"»: название в кавычках с заглавной буквы, аббревиатуры до 3 букв не трогаются."""
    def fix(w: str) -> str:
        core = w.strip('"«»()')
        parts = core.split("-")
        if len(core) <= 3 or not all(x.isalpha() for x in parts):
            return w
        return w.replace(core, "-".join(x.capitalize() for x in parts))
    return " ".join(fix(w) for w in rest.split())


def _cut(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _source_label(o) -> str:
    if o.source == SourceKind.EIS44:
        return f"ЕИС, контракт № {o.contract_number}" if o.contract_number else "ЕИС, реестр контрактов"
    if o.source == SourceKind.MANUAL:
        return "коммерческое предложение"
    return o.source_name or "сайт поставщика"


def _date_label(o) -> str:
    if o.source == SourceKind.WEB:
        return f"получено {dt(o.retrieved_at)}"
    return f"цена от {dt(o.price_date)}"


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    for prefix in ("другой товар: ", "не выполнено: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text[:1].upper() + text[1:]
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


_UNIT_FORMS = {"лист": ("лист", "листа", "листов"), "пачка": ("пачка", "пачки", "пачек"),
               "флакон": ("флакон", "флакона", "флаконов"), "упаковка": ("упаковка", "упаковки", "упаковок"),
               "штука": ("штука", "штуки", "штук"), "комплект": ("комплект", "комплекта", "комплектов"),
               "набор": ("набор", "набора", "наборов"), "коробка": ("коробка", "коробки", "коробок"),
               "рулон": ("рулон", "рулона", "рулонов"), "ампула": ("ампула", "ампулы", "ампул"),
               "бутылка": ("бутылка", "бутылки", "бутылок"), "канистра": ("канистра", "канистры", "канистр"),
               "пара": ("пара", "пары", "пар"), "ящик": ("ящик", "ящика", "ящиков")}


def quantity_label(value: Decimal, unit: str) -> str:
    """«500 листов», «10 мкг», «2 пачки»: число и единица в нужной форме."""
    v = num(value, 3).rstrip("0").rstrip(",") if value != value.to_integral_value() else num(value, 0)
    forms = _UNIT_FORMS.get((unit or "").strip().lower())
    if forms and value == value.to_integral_value():
        return f"{v} {_plural(int(value), *forms)}"
    return f"{v} {unit}"


_ACCUSATIVE = {"штука": "штуку", "пачка": "пачку", "упаковка": "упаковку", "коробка": "коробку", "ампула": "ампулу",
               "бутылка": "бутылку", "банка": "банку", "канистра": "канистру", "туба": "тубу", "пара": "пару",
               "единица": "единицу", "тонна": "тонну", "катушка": "катушку", "паллета": "паллету"}


def _unit_label(unit: str | None) -> str:
    """Единица цены после «за»: «за штуку», «за пачку», «за флакон»."""
    u = (unit or "").strip().lower()
    if not u or u.startswith("не указ"):
        return ""
    return _ACCUSATIVE.get(u, u)


def _qty(q: Decimal) -> str:
    return num(q, 0) if q == q.to_integral_value() else num(q, 3).rstrip("0").rstrip(",")


def _legal(o) -> list[dict[str, Any]]:
    """Итоги правовых проверок цены: неустойки по контракту и запись поставщика в РНП (со ссылками на ЕИС)."""
    if not o.extra.get("legal_checked"):
        return []
    out = []
    if o.source == SourceKind.EIS44 and o.contract_number:
        val = {False: "не взыскивались", True: "взыскивались"}.get(o.penalties, "проверить не удалось")
        out.append({"label": "Неустойки", "value": val, "url": o.extra.get("penalties_url")})
    if o.supplier_inn:
        val = {False: "не значится", True: "включён"}.get(o.extra.get("rnp"), "проверить не удалось")
        out.append({"label": "Реестр недобросовестных поставщиков", "value": val, "url": o.extra.get("rnp_url")})
    return out


def item_view(r: ItemResult) -> dict[str, Any]:
    matches = {m.offer_id: m for m in r.matches}
    norms = {n.offer_id: n for n in r.normalizations}
    decs = {d.offer_id: d for d in r.decisions}
    rows = []
    for o in r.offers:
        m, n, d = matches.get(o.id), norms.get(o.id), decs.get(o.id)
        checks = []
        if m:
            for c in m.checks:
                checks.append({"req": c.requirement, "val": c.offer_value or "—", "status": CHECK_RU.get(c.status, c.status),
                               "cls": c.status.value, "mandatory": c.mandatory, "note": c.note})
        steps = [s.note for s in (n.steps if n else [])]
        rows.append({
            "id": o.id, "source": SOURCE_RU.get(o.source, o.source), "source_name": o.source_name, "url": o.url,
            "contract": o.contract_number, "contract_url": o.extra.get("contract_url"),
            "title": o.title, "title_short": _cut(o.title, 90), "supplier": supplier_label(o.supplier_name or o.source_name),
            "inn": o.supplier_inn,
            "date": dt(o.price_date), "retrieved": dt(o.retrieved_at, with_time=True),
            "price": money(o.price) if o.price else "—", "price_unit": _unit_label(o.price_unit),
            "content": (quantity_label(o.content.value, o.content.unit)
                        if o.content and not (o.content.value == 1 and dimension(canon_unit(o.content.unit)) == "count")
                        else None),
            "unit_price": money(n.unit_price) if n and n.unit_price is not None else "—",
            "unit_price_raw": n.unit_price if n else None,
            "steps": steps, "norm_problem": n.problem if n else None,
            "flags": list(dict.fromkeys(FLAG_RU.get(f, f) for f in (d.flags if d else []) + (n.flags if n else []))),
            "cls": CLASS_RU.get(m.cls, "") if m else "", "cls_key": m.cls.value if m else "",
            "score": f"{int(round((m.score if m else 0) * 100))} %", "reasons": m.reasons if m else [],
            "checks": checks, "included": bool(d and d.included),
            "code": CODE_RU.get(d.code, d.code.value) if d else "", "code_key": d.code.value if d else "",
            "reason": d.reason if d else "", "actor": d.actor if d else "system",
            "note": o.extra.get("note"), "evidence": o.evidence.snapshot_path if o.evidence else None,
            "sha256": o.evidence.sha256 if o.evidence else None, "label": (o.supplier_name or o.source_name)[:60],
            "price_from": o.extra.get("price_from"),
            "source_label": _source_label(o), "date_label": _date_label(o),
            "reason_short": _short(d.reason if d else "", 120),
            "legal": _legal(o),
        })
    included = sorted([x for x in rows if x["included"]], key=lambda x: (x["unit_price_raw"] or 0))
    excluded = [x for x in rows if not x["included"]]
    order = {"UNSUITABLE": 3, "NOT_CONFIRMED": 2, "OUTLIER_MAD": 0, "OUTLIER_RATIO": 0}
    excluded.sort(key=lambda x: (order.get(x["code_key"], 1), x["source"], x["unit_price_raw"] or 0))
    c = r.calc
    calc = None
    labels = {x["id"]: (x["title"] if len(x["title"]) <= 70 else x["title"][:69].rsplit(" ", 1)[0] + "…") for x in rows}
    if c:
        calc = {
            "status": c.status.value, "final": c.status == CalcStatus.FINAL, "message": c.message, "set": c.set_used,
            "n": c.n, "suppliers": c.n_suppliers, "mean": money(c.mean), "median": money(c.median),
            "min": money(c.min), "max": money(c.max), "sd": money(c.sd), "cv": pct(c.cv),
            "unit_price": money(c.unit_price), "total": money(c.total), "method": c.method,
            "prices": [money(p) for p in c.prices],
            "hints": [{"offer": h.without_offer_id, "label": labels.get(h.without_offer_id, h.without_offer_id),
                       "cv": pct(h.cv), "mean": money(h.mean)} for h in c.hints[:5]],
        }
        calc["n_label"] = f"{c.n} {_plural(c.n, 'цена', 'цены', 'цен')}"
        calc["suppliers_label"] = f"{c.n_suppliers} {_plural(c.n_suppliers, 'поставщика', 'поставщиков', 'поставщиков')}"
        head, _, rest = c.message.partition(":")
        calc["headline"] = head.strip() if rest else "Цена не сформирована"
        detail = (rest or c.message).strip()
        calc["detail"] = " " + detail[:1].upper() + detail[1:] if detail else ""
    it = r.item
    reqs = [{"name": q.name, "cond": condition_text(q), "mandatory": q.mandatory, "by_brand": q.by_brand,
             "quote": q.quote} for q in it.requirements]
    return {"item": it, "reqs": reqs, "rows": rows, "included": included, "excluded": excluded, "calc": calc,
            "qty": _qty(it.quantity),
            "qty_label": quantity_label(it.quantity, it.unit),
            "content": (f"{'не менее ' if it.content_op.value == '>=' else ''}{quantity_label(it.content.value, it.content.unit)}"
                        if it.content else None)}


def _decimal_label(value: Any) -> str:
    text = f"{Decimal(str(value)).normalize():f}" if value not in (None, "") else ""
    return text.replace(".", ",")


def _final_label(final: int, n: int) -> str:
    if not final:
        return "нет, нужны дополнительные цены"
    if final == n:
        return "да" if n == 1 else f"по всем {n} позициям"
    return f"по {final} из {n} {_plural(n, 'позиции', 'позиций', 'позиций')}"


def report_view(rep: Report) -> dict[str, Any]:
    items = [item_view(r) for r in rep.items]
    final = sum(1 for r in rep.items if r.calc and r.calc.status == CalcStatus.FINAL)
    st = rep.settings or {}
    rules = st.get("rules") or {}
    method, outliers = rules.get("method") or {}, rules.get("outliers") or {}
    manual = any(o.source == SourceKind.MANUAL for r in rep.items for o in r.offers)
    price_sources = ["реестр контрактов ЕИС", "сайты поставщиков" if st.get("web") else None,
                     "коммерческие предложения" if manual else None]
    calc_date = st.get("calc_date") or ""
    trace = [{"ts_label": dt(e.ts, True).split(" ")[1] if e.ts else "", "message": e.message, "data": e.data}
             for e in rep.trace]
    n = len(items)
    return {"r": rep, "items": items, "final": final, "total": money(rep.total), "created": dt(rep.created_at, True),
            "finished": dt(rep.finished_at, True), "proc": rep.procurement,
            "sources": rep.sources, "llm": rep.llm, "settings": st, "trace": trace,
            "final_label": _final_label(final, n),
            "price_sources": ", ".join(x for x in price_sources if x),
            "parse_label": ("моделью по обезличенному тексту" if rep.procurement.parse_mode == "llm" else
                            "по правилам: модель была недоступна" if st.get("llm") else "по правилам, без модели"),
            "min_suppliers": method.get("min_suppliers", 3),
            "max_age_months": method.get("max_age_months", 36),
            "cpi_months": method.get("cpi_threshold_months", 6),
            "mad_min_n": outliers.get("mad_min_n", 5),
            "cv_limit": pct(Decimal(str(method.get("cv_threshold", "0.33"))), 0),
            "ratio_exclude": _decimal_label(outliers.get("ratio_exclude", "2")),
            "mad_z": _decimal_label(outliers.get("mad_z", "3.5")),
            "calc_date": ".".join(reversed(calc_date.split("-"))) if calc_date else "—"}
