"""Разбор ТЗ без языковой модели: таблицы спецификации, характеристики, условия поставки.

Рассчитан на типовую форму ТЗ (таблица позиций «Номер — Наименование — Единица — Количество — ОКПД2»
и таблица требований), но работает и с простым текстом: тогда получается одна позиция.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.engine.matching import apply_card_rules
from app.engine.rules import CardRules, load_rules
from app.ingest.reader import Document
from app.models import Item, Op, Procurement, Quantity, Requirement

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_CONTENT = re.compile(r"количество листов в (?:пачке|упаковке)|общий вес 1 шт|масса единицы|фасовк", re.I)
_OKPD2 = re.compile(r"\b(\d{2}\.\d{2}\.\d{2}\.\d{3})\b")


def _dec(s: str) -> Optional[Decimal]:
    try:
        return Decimal(s.replace(",", ".").replace(" ", ""))
    except (InvalidOperation, AttributeError):
        return None


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _unit_tail(tail: str) -> Optional[str]:
    """Короткий хвост после числа — единица («страниц», «мм»), длинный текст единицей не считается."""
    t = _norm(tail).strip(" .,;")
    return t if t and len(t) <= 20 and not re.search(r"\d", t) else None


def parse_requirement(rid: str, name: str, value: str, unit: str = "", quote: str = "") -> Requirement:
    """Условие из текста значения: «не менее 10», «более 1 и менее 1,6», «80 ± 3», «142-148», «соответствие»."""
    v = _norm(value)
    name = _norm(name).rstrip(":")
    unit = _norm(unit) or None
    low = v.lower()
    req = dict(id=rid, name=name[:200], unit=unit, quote=(quote or f"{name}: {value}")[:300], mandatory=False)
    m = re.search(rf"не выше\s*({_NUM})\s*и\s*не ниже\s*({_NUM})", low.replace(" -", "-").replace("- ", "-"))
    if m:
        hi, lo = m.group(1), m.group(2)
        return Requirement(op=Op.BETWEEN, value=lo.replace(",", "."), value_max=hi.replace(",", "."), **req)
    m = re.search(rf"(?:более|от)\s*\+?\s*({_NUM})\s*(?:и|до)\s*(?:менее)?\s*\+?\s*({_NUM})", low)
    if m:
        return Requirement(op=Op.BETWEEN, value=m.group(1).replace(",", "."), value_max=m.group(2).replace(",", "."), **req)
    m = re.search(rf"не менее\s*({_NUM})\s*(.*)$", low)
    if m:
        return Requirement(op=Op.GE, value=m.group(1).replace(",", "."),
                           **{**req, "unit": unit or _unit_tail(m.group(2))})
    m = re.search(rf"не более\s*({_NUM})\s*(.*)$", low)
    if m:
        return Requirement(op=Op.LE, value=m.group(1).replace(",", "."),
                           **{**req, "unit": unit or _unit_tail(m.group(2))})
    m = re.search(rf"^менее\s*({_NUM})", low)
    if m:
        return Requirement(op=Op.LT, value=m.group(1).replace(",", "."), **req)
    m = re.search(rf"^более\s*({_NUM})", low)
    if m:
        return Requirement(op=Op.GT, value=m.group(1).replace(",", "."), **req)
    m = re.search(rf"^({_NUM})\s*\(?\s*±\s*({_NUM})\s*(%?)\)?\s*(.*)$", v)
    if m:
        tol = f"±{m.group(2)}{m.group(3)}"
        return Requirement(op=Op.EQ, value=m.group(1).replace(",", "."), tolerance=tol,
                           **{**req, "unit": unit or (_norm(m.group(4)) or None)})
    if re.search(r"\d\s*[xх×]\s*\d", v):
        return Requirement(op=Op.CONTAINS, value=v[:200], **req)
    m = re.search(rf"^({_NUM})\s*[-–]\s*({_NUM})\s*(.*)$", v)
    if m:
        return Requirement(op=Op.BETWEEN, value=m.group(1).replace(",", "."), value_max=m.group(2).replace(",", "."),
                           **{**req, "unit": unit or (_norm(m.group(3)) or None)})
    if low in ("соответствие", "наличие", "есть", "да", "имеется") or low.endswith("- наличие"):
        return Requirement(op=Op.PRESENT, value=None, **req)
    m = re.search(rf"^({_NUM})\s*(.*)$", v)
    if m and len(m.group(2)) < 25:
        return Requirement(op=Op.EQ, value=m.group(1).replace(",", "."),
                           **{**req, "unit": unit or (_norm(m.group(2)) or None)})
    return Requirement(op=Op.CONTAINS, value=v[:200], **req)


def _split_line(line: str) -> tuple[str, str]:
    if ":" in line:
        a, _, b = line.partition(":")
        return a.strip(), b.strip()
    if "-" in line and re.search(r"\s?-\s?наличие$", line):
        a, _, b = line.rpartition("-")
        return a.strip(), b.strip()
    m = re.match(r"^(.{3,80}?[^\d\s])\s*[-–—]\s+(.+)$", line)     # «Технология печати- FDM …»
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(rf"\s({_NUM}.*)$", line)
    if m and m.start() > 3:
        return line[: m.start()].strip(), m.group(1).strip()
    parts = line.rsplit(" ", 1)
    if len(parts) == 2 and len(parts[0]) > 3:
        return parts[0], parts[1]
    return line, "соответствие"


def _words(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-яё0-9]+", (s or "").lower().replace("ё", "е")) if len(w) > 2}


def _mark_essential(reqs: list[Requirement], item_name: str, card: CardRules, by_model: bool) -> None:
    """Существенные характеристики без модели: строка вида товара, затем названия из общего списка правил.

    Длинные описания существенными не делаются; если в ТЗ названы товарный знак и модель, все характеристики
    считаются заданными моделью (by_brand)."""
    starts = [e.lower() for e in card.essential_names]
    name_words = _words(item_name)
    marked = 0
    for r in reqs:
        text = " ".join([r.value or ""] + r.values)
        if r.op == Op.PRESENT and len(r.name) > 20 and name_words and \
                len(_words(r.name) & name_words) >= 0.7 * max(1, len(_words(r.name))):
            # строка повторяет название товара («Интерлейкин 15 человека, рекомбинантный белок») — это вид товара
            r.value, r.op, r.name, r.mandatory = r.name, Op.CONTAINS, "Вид товара", True
            marked += 1
            continue
        if len(r.name) > card.descriptive_len or len(text) > card.descriptive_len:
            continue
        if marked < card.max_essential and any(r.name.lower().startswith(e) for e in starts):
            r.mandatory = True
            marked += 1
    if by_model:
        for r in reqs:
            r.by_brand = True    # уточняется общими правилами карточки (оригинальность моделью не задаётся)


def _search_queries(short: str, brand: Optional[str], model: Optional[str],
                    content: Optional[Quantity]) -> tuple[list[str], list[str]]:
    """Поисковые фразы без модели: полное название, без кода вещества, без знака и модели, знак с моделью."""
    words: list[str] = []
    for w in short.split():
        if w.lower().strip(",.;") in {x.lower().strip(",.;") for x in words} and len(w) <= 3:
            continue          # повтор «3D принтер 3D …»
        words.append(w)
    base = " ".join(words).strip(" ,;")
    code_m = re.match(r"^((?:rh)?[A-Za-z]{1,8}[-\d]+[A-Za-z0-9]*)(?:\s*,\s*|\s+)(?=\w)", base)
    no_code = base[code_m.end():] if code_m else base
    generic = no_code
    for w in (model, brand):
        if w:
            generic = re.sub(re.escape(w), " ", generic, flags=re.I)
    generic = " ".join(generic.split()).strip(" ,;")
    eis: list[str] = []
    first_model = re.split(r"\s*/\s*", model)[0] if model else None
    for q in (base, no_code, generic, f"{brand} {first_model}" if brand and first_model else None):
        if q and len(q) >= 5 and q.lower() not in [x.lower() for x in eis]:
            eis.append(q[:90])
    web: list[str] = []
    if brand and model:
        web.append(f"{brand} {model} цена")
    if code_m:
        fas = f" {content.value.normalize():f} {content.unit}" if content is not None else ""
        web.append(f"{code_m.group(1)}{fas} цена")
    web.append(f"{no_code} купить цена")
    return eis[:3], [q[:120] for q in web[:3]]


_DIM_NAMES = {"area_density": "Плотность", "mass": "Масса", "volume": "Объём", "length": "Размер",
              "power": "Мощность", "sheet": "Количество листов в пачке", "count": "Количество",
              "temperature": "Температура", "percent": "Доля"}
_COLORS = re.compile(r"^(черн|бел|цветн|син|красн|желт|зелен|сер|голуб|пурпур|оранж)", re.I)


def _text_attribute(fragment: str) -> tuple[str, str, str, str]:
    """«ресурс не менее 3000 страниц» → («ресурс», «не менее 3000 страниц»); «80 г/м²» → («Плотность», «80 г/м²»);
    «оригинальный» → («Оригинальность», «оригинальный»); «черный» → («Цвет», «черный»)."""
    f = _norm(fragment)
    m = re.match(rf"^(.*?[A-Za-zА-Яа-яЁё][^\d]*?)\s*((?:не менее|не более|от|до|более|менее)?\s*{_NUM}.*)$", f)
    if m and m.group(1).strip() and not re.search(r"\d", m.group(1)):
        name = m.group(1).strip()
        if name.lower() in ("не менее", "не более", "от", "до", "более", "менее"):
            name = ""
        if name:
            return name, m.group(2).strip(), "", f
    if re.match(rf"^(?:не менее|не более|от|до|более|менее)?\s*{_NUM}", f):
        from app.engine.units import canon_unit, dimension, parse_quantity
        q = parse_quantity(f, prefer="лист" if "лист" in f.lower() else None)
        dim = dimension(canon_unit(q.unit)) if q is not None else None
        return _DIM_NAMES.get(dim or "", "Характеристика"), f, "", f
    if re.search(r"оригинал|совместим|аналог|восстановлен", f, re.I):
        return "Оригинальность", f, "", f
    m = re.match(r"^([А-Яа-яЁё][А-Яа-яЁё ]{2,40}?)\s+([A-Za-zА-Яа-яЁё]{0,3}\d*[A-Za-zА-Яа-яЁё]?)$", f)
    if m and m.group(2) and len(m.group(2)) <= 4:
        return m.group(1).strip(), m.group(2), "", f
    if _COLORS.match(f):
        return "Цвет", f, "", f
    return "Характеристика", f, "", f


_NOT_BRAND = re.compile(r"^(rh|IL|TGF|PLA|ABS|FDM|USB|CIE|ISO|PBS|CHO|ED50|EU|SSD|HDD|RAM|LED|LCD|IPS|HDMI|GOST|ГОСТ)",
                        re.I)


def _brand_model(name: str) -> tuple[Optional[str], Optional[str]]:
    """Товарный знак — первое латинское слово из букв (не короче 4, или 2–3 заглавные перед обозначением модели);
    модель — латинские обозначения после него, артикул в скобках идёт через «/»: «HP 59A (CF259A)» → «59A / CF259A»."""
    spans = list(re.finditer(r"[A-Za-z0-9][A-Za-z0-9\-+]*", name))
    brand_i = None
    for i, m in enumerate(spans):
        t = m.group(0)
        if any(ch.isdigit() for ch in t) or _NOT_BRAND.match(t):
            continue
        nxt = spans[i + 1].group(0) if i + 1 < len(spans) else ""
        if len(t) >= 4 or (t.isupper() and len(t) >= 2 and any(ch.isdigit() for ch in nxt)):
            brand_i = i
            break
    if brand_i is None:
        return None, None
    brand = spans[brand_i].group(0)
    main, alt = [], []
    prev_end = spans[brand_i].end()
    for m in spans[brand_i + 1:]:
        between = name[prev_end:m.start()]
        if re.search(r"[А-Яа-яЁё,;]", between):
            break                                  # кончилось обозначение модели
        (alt if "(" in between or "/" in between else main).append(m.group(0))
        prev_end = m.end()
    parts = [" ".join(main)] + alt if main else alt
    parts = [p for p in parts if p and any(ch.isdigit() for ch in p) or (p and len(p) >= 3 and p.isupper())]
    model = " / ".join(dict.fromkeys(parts)) if parts else None
    return brand, model


def _content_from(req: Requirement) -> tuple[Optional[Quantity], Op]:
    if not _CONTENT.search(req.name) or req.value is None:
        return None, Op.EQ
    val = _dec(req.value)
    if val is None:
        return None, Op.EQ
    unit = (req.unit or "").lower()
    if "лист" in req.name.lower() or not unit or unit in ("шт", "шт.", "штука"):
        unit = "лист" if "лист" in req.name.lower() else unit or "шт"
    return Quantity(value=val, unit=unit), (Op.GE if req.op in (Op.GE, Op.GT) else Op.EQ)


def _find_positions(doc: Document) -> list[dict]:
    for table in doc.tables:
        header_idx = None
        for i, row in enumerate(table[:4]):
            joined = " ".join(row).lower()
            if len(row) >= 3 and "наименование" in joined and ("количество" in joined or "кол-во" in joined):
                header_idx = i
                break
        if header_idx is None:
            continue
        header = [c.lower() for c in table[header_idx]]

        def col(*keys: str) -> Optional[int]:
            for j, h in enumerate(header):
                if any(k in h for k in keys):
                    return j
            return None

        c_num, c_name, c_unit, c_qty, c_okpd = col("номер", "№"), col("наименование"), col("единиц"), \
            col("количество", "кол-во"), col("окпд")
        rows = []
        for row in table[header_idx + 1:]:
            if c_name is None or c_qty is None or len(row) <= max(c_name, c_qty):
                continue
            if all(re.fullmatch(r"\d+", c.strip() or "x") for c in row):
                continue   # строка нумерации столбцов
            qty = _dec(re.sub(r"[^\d.,]", "", row[c_qty]) or "x")
            name = re.split(r"\s*(?:Производитель|Страна происхождения)\b", row[c_name].replace("\n", " "))[0].strip()
            if not name or qty is None:
                continue
            okpd = None
            if c_okpd is not None and c_okpd < len(row):
                m = _OKPD2.search(row[c_okpd])
                okpd = m.group(1) if m else None
            num = len(rows) + 1
            if c_num is not None and c_num < len(row):
                m = re.match(r"\d+", row[c_num].strip())
                num = int(m.group(0)) if m else num
            rows.append({"number": num, "name": _norm(name),
                         "unit": _norm(row[c_unit]) if c_unit is not None and c_unit < len(row) else "шт.",
                         "quantity": qty, "okpd2": okpd})
        if rows:
            return rows
    return []


def _requirements(doc: Document, positions: list[dict]) -> dict[int, list[tuple[str, str, str, str]]]:
    """Сырые характеристики по номеру позиции: (название, значение, единица, цитата)."""
    out: dict[int, list[tuple[str, str, str, str]]] = {p["number"]: [] for p in positions}
    for table in doc.tables:
        if not table:
            continue
        head = " ".join(" ".join(r) for r in table[:2]).lower()
        if "наименование характеристики" in head and "значение" in head:
            last_num = None
            for row in table[2:]:
                if len(row) < 4:
                    continue
                m = re.match(r"(\d+)", row[0].strip())
                num = int(m.group(1)) if m else last_num
                if num is None or num not in out:
                    continue
                last_num = num
                name, value = row[2].replace("\n", " "), row[3].replace("\n", " ")
                unit = row[4].replace("\n", " ") if len(row) > 4 else ""
                if name or value:
                    out[num].append((name, value, unit, f"{name} {value} {unit}".strip()))
        elif "требования" in head and any(len(r) >= 3 for r in table[:2]):
            for row in table[1:]:
                if len(row) < 3:
                    continue
                m = re.match(r"(\d+)", row[0].strip())
                if not m or int(m.group(1)) not in out:
                    continue
                num = int(m.group(1))
                lines = [_norm(x) for x in (row[1] + "\n" + row[2]).split("\n") if _norm(x)]
                merged: list[str] = []
                for line in lines:
                    if merged and merged[-1].endswith(":"):
                        merged[-1] = merged[-1] + " " + line
                    else:
                        merged.append(line)
                pos_name = next((p["name"].lower() for p in positions if p["number"] == num), "")
                for line in merged:
                    if len(line) < 3 or line.lower() in pos_name or line.endswith(":"):
                        continue
                    name, value = _split_line(line)
                    out[num].append((name, value, "", line))
    return out


def parse_procurement(doc: Document) -> Procurement:
    text = doc.text
    title = "Закупка"
    m = re.search(r"Техническое задание\s*\n?\s*(на [^\n]+)", text, re.I)
    if m:
        title = "Закупка " + m.group(1).strip()
    positions = _find_positions(doc)
    notes: list[str] = []
    text_attrs: list[str] = []
    if not positions:
        lines = [_norm(ln) for ln in text.splitlines() if len(ln.strip()) > 5]
        qty_rx = re.compile(rf"[—–-]?\s*({_NUM})\s*(шт\w*|пач\w*|упак\w*|комплект\w*|флакон\w*|коробк\w*|рулон\w*)\.?\s*$",
                            re.I)
        line = next((ln for ln in lines if qty_rx.search(ln)), lines[0] if lines else "Товар")
        qty, unit = Decimal("1"), "шт."
        qm = qty_rx.search(line)
        if qm:
            qty = _dec(qm.group(1)) or Decimal("1")
            unit = {"шт": "шт.", "пач": "пачка", "упа": "упаковка", "ком": "комплект", "фла": "флакон",
                    "кор": "коробка", "рул": "рулон"}.get(qm.group(2)[:3].lower(), "шт.")
            line = line[: qm.start()].strip(" ,;—–-")
        parts = [x.strip() for x in re.split(r",\s*(?![^()]*\))", line) if x.strip()]
        name = parts[0] if parts else line
        text_attrs = parts[1:]
        # «Картридж HP 59A (CF259A) оригинальный»: признак, приписанный к названию без запятой, — отдельное требование
        om = re.search(r"\s((?:не)?оригинальн\w*|совместим\w*|восстановленн\w*)(?=\s|$)", name, re.I)
        if om:
            text_attrs.insert(0, om.group(1))
            name = " ".join((name[:om.start()] + name[om.end():]).split())
        cm = re.search(r"\s((?:ч[её]рн|бел|цветн|син|красн|ж[её]лт|зел[её]н|сер|голуб|пурпурн|оранжев)"
                       r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие))$", name, re.I)
        if cm and len(name.split()) > 2:
            text_attrs.insert(0, cm.group(1))
            name = name[:cm.start()].strip()
        positions = [{"number": 1, "name": name[:200], "unit": unit, "quantity": qty, "okpd2": None}]
        notes.append("Таблица позиций не найдена: описание разобрано как одна позиция.")
        first = lines[0] if lines else ""
        if title == "Закупка":
            title = first.rstrip(".") if len(lines) > 1 and first and not qty_rx.search(first) else f"Закупка — {name}"
    raw = _requirements(doc, positions)
    if text_attrs:
        fm = re.search(r"(?<![A-Za-zА-Яа-яЁё0-9])([АA][0-6])(?![A-Za-zА-Яа-яЁё0-9])", positions[0]["name"])
        extra = [("Формат", fm.group(1), "", fm.group(1))] if fm else []
        raw[1] = raw.get(1, []) + extra + [_text_attribute(a) for a in text_attrs]
    card = load_rules().card
    items: list[Item] = []
    for p in positions:
        reqs: list[Requirement] = []
        content, content_op = None, Op.EQ
        brand, model = _brand_model(p["name"])
        for k, (name, value, unit, quote) in enumerate(raw.get(p["number"], []), start=1):
            if re.match(r"(страна происхождения|производитель)", name, re.I):
                if name.lower().startswith("производитель") and value and not brand:
                    brand = value.strip()
                continue
            req = parse_requirement(f"p{p['number']}r{k}", name, value, unit, quote)
            c, cop = _content_from(req)
            if c is not None:
                content, content_op = c, cop
                continue
            reqs.append(req)
        if content is None:
            cm = re.search(r"(\d+)\s*(?:л\.|листов)", p["name"])
            if cm:
                content = Quantity(value=Decimal(cm.group(1)), unit="лист")
        short = re.sub(r"\s*\(.*?\)", "", p["name"]).strip()
        _mark_essential(reqs, p["name"], card, by_model=bool(brand and model))
        eis_q, web_q = _search_queries(short, brand, model, content)
        items.append(apply_card_rules(Item(
            id=f"item{p['number']}", number=p["number"], name=p["name"], short_name=short[:120],
            brand=brand, model=model, okpd2=p.get("okpd2"), quantity=p["quantity"], unit=p["unit"],
            content=content, content_op=content_op, requirements=reqs,
            eis_queries=eis_q, web_queries=web_q, search_queries=eis_q,
        )))
    delivery_included = bool(re.search(r"с включением всех|доставка входит в (стоимость|цену)|включая доставку", text, re.I))
    place = None
    pm = re.search(r"(?:по адресу|до адреса)\s*:\s*([^\n]{10,200})", text, re.I) or \
        re.search(r"(?:по адресу|до адреса)\s*([^\n]{10,200})", text, re.I)
    if pm:
        place = pm.group(1).strip().rstrip(".")
    terms = None
    tm = re.search(r"(В течение \d+[^|\n]{0,120})", text, re.I)
    if tm:
        terms = tm.group(1).strip()
    return Procurement(title=title, items=items, delivery_place=place, delivery_terms=terms,
                       delivery_included=delivery_included or None, source_name=doc.name,
                       source_sha256=doc.sha256, parse_mode="rules", notes=notes)
