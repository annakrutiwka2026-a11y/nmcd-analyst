"""Сверка предложения с требованиями ТЗ по существенным характеристикам.

Каждое требование проверяется отдельно:
- числа, диапазоны и границы источника («≥ 160 и < 180 г/м²», «не менее 90 %») — с переводом единиц
  и допуском из ТЗ;
- габариты «600×600×600» — поэлементно;
- текст — в двух канонических формах: латинские двойники кириллицы («А4» = «A4») и транслитерация
  («3Д» = «3D», «Светокопи» = «SvetoCopy»);
- «наличие» — по утвердительным значениям.
Характеристика источника подбирается к требованию по смыслу названия и по совместимости единиц.
Итог: идентичное, однородное или неподходящее — с причинами по-русски.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from app.engine.rules import Rules
from app.engine.units import canon_unit, convert, dimension, fmt_num, parse_number, parse_quantity, scan_quantities
from app.models import CharCheck, CheckStatus, Item, MatchClass, MatchResult, Offer, OfferChar, Op, Requirement

# ---------------------------------------------------------------- канонические формы текста

_LAT2CYR = str.maketrans("abcehkmoptxy", "авсенкмортху")
_CYR2LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
            "ю": "yu", "я": "ya"}
_DIM_SEP = re.compile(r"(?<=\d)\s*[xх×*]\s*(?=\d)", re.I)
_SEPS = re.compile(r"[\s\-–—_.,;:()\"'«»/\\]+")
_STOP = {"и", "в", "на", "с", "по", "для", "из", "от", "до", "не", "или", "при", "the", "of", "with", "and", "for"}
_YES = {"да", "есть", "наличие", "соответствие", "соответствует", "поддерживается", "имеется", "присутствует", "yes",
        "true", "+", "включено", "включен", "в наличии", "предусмотрен", "предусмотрено"}
_NO = {"нет", "отсутствует", "не поддерживается", "no", "false", "-", "не имеется", "не предусмотрено"}
_MOD_MARKS = {"plus", "max", "mini", "lite", "hs", "se", "ultra", "pro", "neo", "air", "evo", "s", "x", "l", "xl"}


def lookalike(s: str) -> str:
    """Нижний регистр, латинские двойники → кириллица, без пробелов и знаков («A4» → «а4»)."""
    s = _DIM_SEP.sub("х", (s or "").lower().replace("ё", "е")).translate(_LAT2CYR)
    return _SEPS.sub("", s)


def phonetic(s: str) -> str:
    """Транслитерация в латиницу с выравниванием созвучий: «3Д» → «3d», «Светокопи» и «SvetoCopy» → «svetokopi»."""
    s = _DIM_SEP.sub("x", (s or "").lower().replace("ё", "е"))
    s = "".join(_CYR2LAT.get(ch, ch) for ch in s)
    s = (s.replace("ph", "f").replace("ck", "k").replace("c", "k").replace("q", "k").replace("w", "v")
         .replace("y", "i").replace("x", "ks"))
    return re.sub(r"[^a-z0-9]+", "", s)


def canon_text(s: str) -> str:
    """Каноническая форма для сравнения строк (латинские двойники → кириллица)."""
    return lookalike(s)


def _contains(hay: str, needle: str) -> bool:
    for form in (lookalike, phonetic):
        n = form(needle)
        if n and n in form(hay):
            return True
    return False


def _token_list(s: str) -> list[str]:
    s = re.sub(r"(?<=\d),(?=\d)", ".", _DIM_SEP.sub("x", (s or "").lower()))
    return [t for t in re.split(r"[\s,;:()\[\]\"'«»/\\|]+", s) if t]


def _has_token(hay: str, needle: str) -> bool:
    """Короткое значение («С», «А4», «FDM») ищется как отдельное слово."""
    n1, n2 = lookalike(needle), phonetic(needle)
    for t in _token_list(hay):
        t = t.strip(".-–—")
        if t and (lookalike(t) == n1 or (n2 and phonetic(t) == n2)):
            return True
    return False


_ENDINGS = ("ого", "его", "ому", "ему", "ыми", "ими", "ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю",
            "ых", "их", "ым", "им", "ом", "ем", "ой", "ей", "ам", "ям", "ах", "ях", "ов", "ев", "ы", "и", "а", "я", "о",
            "е", "у", "ю")


def _stem_ru(w: str) -> str:
    """Отбрасывает падежное окончание русского слова (основа не короче трёх букв)."""
    if not re.fullmatch(r"[а-я]+", w):
        return w
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[: -len(e)]
    return w


def _words(s: str, synonyms: dict[str, str]) -> list[str]:
    out: list[str] = []
    for w in re.findall(r"[a-zа-яё0-9]+", _DIM_SEP.sub("x", (s or "").lower())):
        w = w.replace("ё", "е")
        if w in _STOP:
            continue
        w = _stem_ru(w)
        for prefix, repl in synonyms.items():
            if w.startswith(prefix):
                w = repl
                break
        p = phonetic(w)
        if p:
            out.append(p)
    return out


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return min(len(a), len(b)) >= 5 and a[:5] == b[:5]


def name_similarity(a: str, b: str, synonyms: dict[str, str], drop: Iterable[str] = ()) -> float:
    """Доля совпавших слов (по основам) от меньшего из названий; слова из `drop` (вид товара) не учитываются."""
    drop = list(drop)
    wa_all, wb_all = set(_words(a, synonyms)), set(_words(b, synonyms))
    wa = {w for w in wa_all if not any(_same_word(w, d) for d in drop)} or wa_all
    wb = {w for w in wb_all if not any(_same_word(w, d) for d in drop)} or wb_all
    if not wa or not wb:
        return 0.0
    hit = sum(1 for x in wa if any(_same_word(x, y) for y in wb))
    return hit / max(1, min(len(wa), len(wb)))


_ORIGIN_RX = re.compile(r"оригинал|совместим|аналог|эквивалент|восстановлен|\bнов(ый|ое|ая|ые)\b|б/у|бывш|состояни",
                        re.I)
_COMPAT_RX = re.compile(r"совместим|аналог|compatible|неоригинал|эквивалент|восстановлен|refill|заправ", re.I)


def is_origin_requirement(req: Requirement) -> bool:
    """Требование к происхождению или состоянию товара (оригинальный, новый): номер модели его не задаёт."""
    return bool(_ORIGIN_RX.search(f"{req.name} {req.value or ''} {' '.join(req.values)}"))


def apply_card_rules(item: Item) -> Item:
    """Общие правила карточки: «оригинальный» — существенная характеристика; характеристики считаются заданными
    только моделью (знак и модель названы вместе), кроме оригинальности и состояния товара. Один товарный знак
    характеристик не задаёт: у одного знака бывают и А4, и А3."""
    by_model = bool(item.brand and item.model)
    for r in item.requirements:
        origin = is_origin_requirement(r)
        if origin and re.search(r"оригинал", f"{r.value or ''} {' '.join(r.values)}", re.I):
            r.mandatory = True
        r.by_brand = by_model and not origin
    return item


def head_terms(item: Item) -> list[str]:
    """Основное слово названия позиции («бумага», «принтер»): в названиях характеристик оно не различает смысл."""
    for w in re.findall(r"[a-zа-яё]+", (item.short_name or item.name or "").lower()):
        if len(w) >= 4 and w not in _STOP:
            return [phonetic(w)]
    return []


# ---------------------------------------------------------------- числа, границы, габариты

@dataclass
class Bounds:
    lo: Optional[Decimal]
    hi: Optional[Decimal]
    unit: Optional[str]

    @property
    def exact(self) -> bool:
        return self.lo is not None and self.lo == self.hi


_OP_WORDS = [(r"не\s+менее|не\s+ниже|не\s+меньше|минимум|мин\.?|min\.?|от", "≥"),
             (r"не\s+более|не\s+выше|не\s+больше|максимум|макс\.?|max\.?|до", "≤"),
             (r"более|свыше|выше|больше", ">"), (r"менее|ниже|меньше", "<")]
_NUM_RE = r"\d+(?:[.,]\d+)?"


def _triple(text: str) -> Optional[tuple[Decimal, ...]]:
    m = re.search(rf"({_NUM_RE})\s*[xх×*]\s*({_NUM_RE})(?:\s*[xх×*]\s*({_NUM_RE}))?", text or "", re.I)
    if not m:
        return None
    vals = [parse_number(g) for g in m.groups() if g]
    if any(v is None for v in vals):
        return None
    return tuple(vals)  # type: ignore[arg-type]


def parse_bounds(value: str, unit: Optional[str] = None, prefer: Optional[str] = None) -> Optional[Bounds]:
    """Число, диапазон или граница из значения характеристики. None — значение не числовое («А4», «Tween20»)."""
    s = " ".join((value or "").lower().replace("ё", "е").split())
    if not s or _triple(s):
        return None
    s = re.sub(r"(?<=[мm])2(?!\d)", "²", re.sub(r"(?<=[мm])3(?!\d)", "³", s))   # «г/м2», «см3»
    s = re.sub(r"(?<=\d)\s*(?:[-–—]|\.\.\.|…)\s*(?=-?\d)", " ~ ", s)
    for pattern, sym in _OP_WORDS:
        s = re.sub(rf"(?<![a-zа-я]){pattern}(?![a-zа-я])", f" {sym} ", s)
    s = s.replace(">=", "≥").replace("<=", "≤").replace("+/-", "±")
    tokens = re.findall(rf"≥|≤|>|<|±|~|-?{_NUM_RE}|[^\s\d≥≤<>±~]+", s)
    nums: list[tuple[Optional[str], Decimal]] = []
    op: Optional[str] = None
    tail: list[str] = []
    pm = False
    for t in tokens:
        if re.fullmatch(rf"-?{_NUM_RE}", t):
            if tail and not nums:
                return None
            n = parse_number(t)
            if n is None:
                return None
            nums.append(("±" if pm else op, n))
            op, pm = None, False
            continue
        if t in ("≥", "≤", ">", "<"):
            op = t
        elif t == "±":
            pm = True
        elif t == "~":
            op = "~"
        elif t in ("и", ",", ";", "и/или", "в", "диапазоне", "пределах", "включительно", "("):
            continue
        else:
            if not nums:
                return None           # слово перед числом: «А4», «IL-15»
            tail.append(t)
    if not nums or len(nums) > 2:
        return None
    u = canon_unit(unit) if unit else None
    if tail:
        tail_text = " ".join(tail).strip(" .,;)")
        tu = canon_unit(tail_text)
        if tu is None or (dimension(tu) is None and not unit):
            q = parse_quantity(f"1 {tail_text}", prefer=prefer)
            tu = canon_unit(q.unit) if q is not None else tu
        if (tu is None or dimension(tu) is None) and len(tail_text) > 20:
            return None
        u = u or tu
    if len(nums) == 1:
        o, n = nums[0]
        if o in ("≥", ">"):
            return Bounds(n, None, u)
        if o in ("≤", "<"):
            return Bounds(None, n, u)
        return Bounds(n, n, u)
    (o1, a), (o2, b) = nums
    if o2 == "±":
        return Bounds(a - abs(b), a + abs(b), u)
    if o1 is None and o2 is None:
        return None                   # перечисление вариантов («10 мкг, 50 мкг»), а не диапазон
    return Bounds(min(a, b), max(a, b), u)


def _req_unit_for(unit: Optional[str], req: Requirement) -> tuple[Optional[Decimal], str]:
    """Множитель перевода из единицы источника в единицу требования (None — единицы несовместимы)."""
    ru = canon_unit(req.unit) if req.unit else None
    if not unit or not ru or unit == ru:
        return Decimal(1), ""
    du, dr = dimension(unit), dimension(ru)
    if du and dr and du == dr:
        k = convert(Decimal(1), unit, ru)
        if k is not None:
            return k, f"переведено из {unit} в {ru}"
    if ru == "г/м²" and unit == "г":
        return Decimal(1), "в источнике «г», по смыслу г/м²"
    if du and dr and du != dr:
        return None, f"другая единица ({unit} вместо {ru})"
    return Decimal(1), ""


def _half_step(value: Optional[str]) -> Decimal:
    """Половина единицы последнего знака числа в ТЗ: «15» → 0,5; «1,75» → 0,005 (значение записано с этой точностью)."""
    m = re.search(r"\d+(?:[.,]\d+)?", value or "")
    if not m:
        return Decimal(0)
    exp = Decimal(m.group(0).replace(",", ".")).as_tuple().exponent
    return Decimal(1).scaleb(int(exp)) / 2 if isinstance(exp, int) else Decimal(0)


def _tolerance(req: Requirement, ref: Decimal, default_rel: Decimal) -> Decimal:
    tol = (req.tolerance or "").replace(",", ".")
    m = re.search(r"±?\s*([\d.]+)\s*(%?)", tol)
    if not m:
        return max(abs(ref) * default_rel, _half_step(req.value))
    val = Decimal(m.group(1))
    return abs(ref) * val / 100 if m.group(2) else val


def _judge(req: Requirement, b: Bounds, delta: Decimal, default_rel: Decimal) -> tuple[CheckStatus, str]:
    k, why = _req_unit_for(b.unit, req)
    if k is None:
        return CheckStatus.UNCLEAR, why
    lo = b.lo * k if b.lo is not None else None
    hi = b.hi * k if b.hi is not None else None
    r = parse_number(req.value or "")
    if r is None:
        return CheckStatus.UNCLEAR, "в требовании нет числа"
    note = why
    op = req.op
    if op == Op.EQ:
        tol = _tolerance(req, r, default_rel)
        a, z = r - tol, r + tol
        if lo is not None and hi is not None:
            if a <= lo and hi <= z:
                return CheckStatus.OK, note
            if hi < a or lo > z:
                return CheckStatus.FAIL, note or f"отличается от {fmt_num(r)}"
            return CheckStatus.UNCLEAR, "диапазон в источнике шире требования"
        if lo is not None:
            if a <= lo <= z:
                return CheckStatus.OK, f"в источнике «не менее {fmt_num(lo)}»"
            return (CheckStatus.FAIL, f"в источнике не менее {fmt_num(lo)}") if lo > z else (
                CheckStatus.UNCLEAR, "в источнике указана только нижняя граница")
        if hi is not None:
            if a <= hi <= z:
                return CheckStatus.OK, f"в источнике «не более {fmt_num(hi)}»"
            return (CheckStatus.FAIL, f"в источнике не более {fmt_num(hi)}") if hi < a else (
                CheckStatus.UNCLEAR, "в источнике указана только верхняя граница")
        return CheckStatus.UNCLEAR, "значение не число"
    if op == Op.BETWEEN:
        r2 = parse_number(req.value_max or "")
        if r2 is None:
            return CheckStatus.UNCLEAR, "в требовании нет верхней границы"
        a, z = min(r, r2), max(r, r2)
        if lo is not None and hi is not None:
            if a <= lo and hi <= z:
                return CheckStatus.OK, note
            if hi < a or lo > z:
                return CheckStatus.FAIL, note or "вне диапазона"
            return CheckStatus.UNCLEAR, "диапазон источника частично выходит за требование"
        if lo is not None and lo > z:
            return CheckStatus.FAIL, "вне диапазона"
        if hi is not None and hi < a:
            return CheckStatus.FAIL, "вне диапазона"
        return CheckStatus.UNCLEAR, "в источнике указана только одна граница"
    if op in (Op.GE, Op.GT):
        sat = (lambda v: v >= r) if op == Op.GE else (lambda v: v > r)
        if lo is not None:
            if sat(lo):
                if r > 0 and lo > r * (1 + delta):
                    return CheckStatus.ABOVE, note or "с запасом сверх требования"
                return CheckStatus.OK, note
            if hi is not None and not sat(hi):
                return CheckStatus.FAIL, note or "меньше требуемого"
            return CheckStatus.UNCLEAR, "значение в источнике может быть меньше требуемого"
        if hi is not None:
            return (CheckStatus.FAIL, "меньше требуемого") if not sat(hi) else (
                CheckStatus.UNCLEAR, "в источнике указана только верхняя граница")
        return CheckStatus.UNCLEAR, "значение не число"
    if op in (Op.LE, Op.LT):
        sat = (lambda v: v <= r) if op == Op.LE else (lambda v: v < r)
        if hi is not None:
            if sat(hi):
                if r > 0 and hi < r * (1 - delta):
                    return CheckStatus.ABOVE, note or "с запасом сверх требования"
                return CheckStatus.OK, note
            if lo is not None and not sat(lo):
                return CheckStatus.FAIL, note or "больше допустимого"
            return CheckStatus.UNCLEAR, "значение в источнике может быть больше допустимого"
        if lo is not None:
            return (CheckStatus.FAIL, "больше допустимого") if not sat(lo) else (
                CheckStatus.UNCLEAR, "в источнике указана только нижняя граница")
        return CheckStatus.UNCLEAR, "значение не число"
    return CheckStatus.UNCLEAR, ""


def _judge_triple(req: Requirement, have: tuple[Decimal, ...], unit: Optional[str],
                  delta: Decimal) -> tuple[CheckStatus, str]:
    need = _triple(req.value or "")
    if need is None or len(need) != len(have):
        return CheckStatus.UNCLEAR, "другое число измерений"
    k, why = _req_unit_for(unit, req)
    if k is None:
        return CheckStatus.UNCLEAR, why
    hv = sorted(v * k for v in have)
    nv = sorted(need)
    tol = _tolerance(req, max(nv), Decimal(0)) if req.tolerance else Decimal(0)
    shown = "×".join(fmt_num(v) for v in have)
    if req.op in (Op.GE, Op.GT):
        if all(h >= n for h, n in zip(hv, nv)):
            if any(h > n * (1 + delta) for h, n in zip(hv, nv)):
                return CheckStatus.ABOVE, f"{shown}: с запасом сверх требования"
            return CheckStatus.OK, why
        return CheckStatus.FAIL, f"в источнике {shown}"
    if req.op in (Op.LE, Op.LT):
        if all(h <= n for h, n in zip(hv, nv)):
            return CheckStatus.OK, why
        return CheckStatus.FAIL, f"в источнике {shown}"
    if all(abs(h - n) <= tol for h, n in zip(hv, nv)):
        return CheckStatus.OK, why
    return CheckStatus.FAIL, f"в источнике {shown}"


# ---------------------------------------------------------------- текст

def _code_like(s: str) -> bool:
    c = lookalike(s)
    return 0 < len(c) <= 12 and len((s or "").split()) <= 2


def _text_status(need: str, have: str, synonyms: dict[str, str]) -> CheckStatus:
    if not lookalike(need):
        return CheckStatus.UNCLEAR
    nb, hb = parse_bounds(need), parse_bounds(have)
    if nb is not None and hb is not None and nb.exact and hb.exact:
        k = convert(Decimal(1), hb.unit, nb.unit) if hb.unit and nb.unit and hb.unit != nb.unit else Decimal(1)
        if k is not None:
            return CheckStatus.OK if hb.lo * k == nb.lo else CheckStatus.FAIL
    if len(lookalike(need)) <= 2:
        return CheckStatus.OK if _has_token(have, need) else (
            CheckStatus.FAIL if _code_like(have) else CheckStatus.UNCLEAR)
    if _contains(have, need):
        return CheckStatus.OK
    if len(lookalike(have)) >= 3 and _contains(need, have) and not _code_like(need):
        return CheckStatus.OK
    words = [w for w in _words(need, synonyms) if len(w) > 2]
    if words:
        hw = _words(have, synonyms)
        hit = sum(1 for w in words if any(_same_word(w, x) for x in hw))
        if hit == len(words) or (len(words) >= 2 and hit / len(words) >= 0.7):
            return CheckStatus.OK
    codes = set(re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", need))
    if codes and _code_like(have) and codes & {w.upper() for w in re.findall(r"[A-Za-z0-9]+", have)}:
        return CheckStatus.OK
    if _code_like(need) and _code_like(have):
        return CheckStatus.FAIL
    return CheckStatus.UNCLEAR


def _hay_has(need: str, title: str, text: str, synonyms: dict[str, str], structured: bool) -> bool:
    """Значение требования встречается в названии или (для реестра) в тексте источника."""
    if len(lookalike(need)) <= 2:
        return _has_token(title, need) or (structured and _has_token(text, need))
    hay = f"{title} {text}"
    if _contains(hay, need):
        return True
    words = [w for w in _words(need, synonyms) if len(w) > 2]
    if len(words) >= 3:
        hw = set(_words(hay, synonyms))
        hit = sum(1 for w in words if any(_same_word(w, x) for x in hw))
        return hit / len(words) >= 0.7
    return False


# ---------------------------------------------------------------- подбор характеристики

def _is_numeric_req(req: Requirement) -> bool:
    if req.op not in (Op.EQ, Op.GE, Op.LE, Op.GT, Op.LT, Op.BETWEEN):
        return False
    v = req.value or ""
    return parse_number(v) is not None and _triple(v) is None and not re.search(r"[a-zа-яё]", v, re.I)


def _char_bounds(ch: OfferChar, req: Requirement) -> Optional[Bounds]:
    prefer = canon_unit(req.unit) if req.unit else None
    return parse_bounds(ch.value or "", ch.unit, prefer)


def _compatible(req: Requirement, ch: OfferChar, numeric: bool, triple: bool) -> bool:
    if triple:
        return _triple(ch.value or "") is not None
    b = _char_bounds(ch, req)
    if numeric:
        if b is None:
            return False
        ru = canon_unit(req.unit) if req.unit else None
        if ru and b.unit and dimension(ru) and dimension(b.unit) and dimension(ru) != dimension(b.unit) and \
                not (ru == "г/м²" and b.unit == "г"):
            return False
        return True
    # текстовое требование не сопоставляется с чистым числом с единицей («80 г/м²»)
    if b is not None and b.unit and dimension(b.unit) and not re.search(r"\d", " ".join([req.value or ""] + req.values)):
        return False
    return True


def _candidates(req: Requirement, offer: Offer, rules: Rules, drop: list[str],
                numeric: bool, triple: bool) -> list[tuple[OfferChar, str]]:
    m = rules.matching
    reject = m.unverified_quote == "reject"
    out: list[tuple[OfferChar, str]] = []
    for ch in offer.characteristics:
        if ch.requirement_id == req.id and not (reject and ch.quote and not ch.quote_verified):
            out.append((ch, "model"))
    raw = [ch for ch in offer.characteristics if not ch.requirement_id]
    scored = []
    for ch in raw:
        s = name_similarity(req.name, ch.name, m.token_synonyms, drop)
        if s >= float(m.name_similarity) and _compatible(req, ch, numeric, triple):
            scored.append((s, ch))
    scored.sort(key=lambda t: -t[0])
    out += [(ch, "name") for _, ch in scored[:2]]
    ru = canon_unit(req.unit) if req.unit else None
    if ru and ru in [canon_unit(u) for u in m.pair_by_unit]:
        for ch in raw:
            b = _char_bounds(ch, req)
            if b is not None and b.unit == ru and all(ch is not c for c, _ in out):
                out.append((ch, "unit"))
    return out


# ---------------------------------------------------------------- проверка требования

@dataclass
class Identity:
    brand: Optional[bool] = None    # None — в ТЗ нет товарного знака
    model: Optional[bool] = None    # None — в ТЗ нет модели

    @property
    def confirmed(self) -> bool:
        """Товар той же модели: модель из ТЗ названа точно, товарный знак (если указан) совпал."""
        return self.model is True and self.brand is not False


def _evaluate(req: Requirement, ch: OfferChar, rules: Rules, numeric: bool, triple: bool) -> tuple[CheckStatus, str]:
    syn = rules.matching.token_synonyms
    if triple:
        have = _triple(ch.value or "")
        if have is None:
            return CheckStatus.UNCLEAR, "размеры не распознаны"
        unit = canon_unit(ch.unit) if ch.unit else None
        if unit is None:
            q = parse_quantity(re.sub(r"[xх×*]", " ", ch.value or ""), prefer=canon_unit(req.unit) if req.unit else None)
            unit = canon_unit(q.unit) if q is not None else None
        return _judge_triple(req, have, unit, rules.matching.identical_delta)
    if numeric:
        b = _char_bounds(ch, req)
        if b is None:
            return CheckStatus.UNCLEAR, "значение не число"
        return _judge(req, b, rules.matching.identical_delta, rules.matching.eq_tolerance)
    value = ch.value or ""
    if req.op == Op.PRESENT:
        low = value.strip().lower()
        if low in _YES or _text_status(req.name, value, syn) == CheckStatus.OK:
            return CheckStatus.OK, ""
        return (CheckStatus.FAIL, "") if low in _NO else (CheckStatus.UNCLEAR, "")
    if req.op == Op.IN:
        statuses = [_text_status(v, value, syn) for v in req.values]
        if CheckStatus.OK in statuses:
            return CheckStatus.OK, ""
        return (CheckStatus.FAIL, "") if statuses and all(s == CheckStatus.FAIL for s in statuses) else (
            CheckStatus.UNCLEAR, "")
    return _text_status(req.value or "", value, syn), ""


def check_requirement(req: Requirement, offer: Offer, rules: Rules, verdict: Optional[str] = None,
                      drop: Optional[list[str]] = None, identity: Optional[Identity] = None) -> tuple[CharCheck, bool]:
    """Проверка одного требования. Возвращает результат и признак того, что решила модель."""
    syn = rules.matching.token_synonyms
    triple = _triple(req.value or "") is not None and req.op in (Op.EQ, Op.GE, Op.LE, Op.GT, Op.LT)
    numeric = not triple and _is_numeric_req(req)
    status, note, value = CheckStatus.MISSING, "", None
    if re.search(r"оригинал", f"{req.value or ''} {' '.join(req.values)}", re.I) and _COMPAT_RX.search(offer.title or ""):
        m = _COMPAT_RX.search(offer.title or "")
        return CharCheck(requirement_id=req.id, requirement=_describe(req), offer_value=f"в названии: «{m.group(0)}»",
                         status=CheckStatus.FAIL, mandatory=req.mandatory,
                         note="совместимый товар или аналог, а не оригинальный"), False
    first_unclear: Optional[tuple[CheckStatus, str, str]] = None
    for ch, how in _candidates(req, offer, rules, drop or [], numeric, triple):
        st, nt = _evaluate(req, ch, rules, numeric, triple)
        shown = f"{ch.value} {ch.unit or ''}".strip()
        if how != "model" and ch.name and ch.name.strip().lower() != req.name.strip().lower():
            shown = f"{ch.name}: {shown}"
        if st in (CheckStatus.OK, CheckStatus.ABOVE, CheckStatus.FAIL):
            status, note, value = st, nt, shown
            first_unclear = None
            break
        if first_unclear is None:
            first_unclear = (st, nt, shown)
    if first_unclear is not None:
        status, note, value = first_unclear
    title, text = offer.title or "", offer.text or ""
    structured = offer.source.value.startswith("eis")
    if status == CheckStatus.MISSING and rules.matching.use_title:
        if triple:
            need = _triple(req.value or "")
            for src in (title, text):
                for m in re.finditer(rf"{_NUM_RE}\s*[xх×*]\s*{_NUM_RE}(?:\s*[xх×*]\s*{_NUM_RE})?", src, re.I):
                    have = _triple(m.group(0))
                    if have is not None and need is not None and sorted(have) == sorted(need):
                        status, value, note = CheckStatus.OK, m.group(0), "по тексту источника"
                        break
                if status != CheckStatus.MISSING:
                    break
        elif numeric and req.unit:
            ru = canon_unit(req.unit)
            _, found = scan_quantities(title, prefer=ru)
            same = [f for f in found if f.unit and dimension(f.unit) and dimension(f.unit) == dimension(ru)]
            values = {f.value * (_req_unit_for(f.unit, req)[0] or 1) for f in same}
            if len(values) == 1:
                f = same[0]
                st, nt = _judge(req, Bounds(f.value, f.value, f.unit), rules.matching.identical_delta,
                                rules.matching.eq_tolerance)
                status, value, note = st, f"{fmt_num(f.value)} {f.unit} (в названии)", nt or "по названию товара"
        elif not numeric:
            if req.op == Op.IN:
                if any(_hay_has(v, title, text, syn, structured) for v in req.values):
                    status, value, note = CheckStatus.OK, "найдено в описании", "по тексту источника"
            elif req.op == Op.PRESENT:
                if _hay_has(req.name, title, text, syn, structured):
                    status, value, note = CheckStatus.OK, "найдено в описании", "по тексту источника"
            elif req.value and _hay_has(req.value, title, text, syn, structured):
                status, value, note = CheckStatus.OK, "найдено в описании", "по тексту источника"
    used_llm = False
    if verdict and rules.matching.model_confirms and status in (CheckStatus.MISSING, CheckStatus.UNCLEAR):
        mapped = {"ok": CheckStatus.OK, "fail": CheckStatus.FAIL}.get(verdict)
        # по числу без процитированного значения вердикт модели не принимается (ни «да», ни «нет»)
        if mapped is not None and not ((numeric or triple) and status == CheckStatus.MISSING):
            status, used_llm = mapped, True
            note = (note + "; " if note else "") + "по оценке модели"
    if status in (CheckStatus.MISSING, CheckStatus.UNCLEAR) and req.by_brand and identity is not None \
            and identity.confirmed:
        status, note = CheckStatus.OK, "задано товарным знаком и моделью из ТЗ"
        informative = value and not ((numeric or triple) and not re.search(r"\d", value))
        value = f"{value}; определяется моделью" if informative else "не указано, определяется моделью"
    return CharCheck(requirement_id=req.id, requirement=_describe(req), offer_value=value, status=status,
                     mandatory=req.mandatory, note=note), used_llm


def condition_text(req: Requirement) -> str:
    """Условие требования словами: «не менее 10 мкг», «80 ±3 г/м²», «от 0,05 до 0,4 мм»."""
    unit = f" {req.unit}" if req.unit else ""
    tol = f" {req.tolerance}" if req.tolerance else ""
    v = req.value or ""
    words = {Op.EQ: f"{v}{tol}{unit}", Op.GE: f"не менее {v}{unit}", Op.LE: f"не более {v}{unit}",
             Op.GT: f"более {v}{unit}", Op.LT: f"менее {v}{unit}", Op.BETWEEN: f"от {v} до {req.value_max}{unit}",
             Op.IN: "одно из: " + ", ".join(req.values), Op.CONTAINS: f"содержит «{v}»", Op.PRESENT: "наличие"}
    return words.get(req.op, v).strip()


def _describe(req: Requirement) -> str:
    return f"{req.name}: {condition_text(req)}".strip()


# ---------------------------------------------------------------- товарный знак и модель

def _brand_ok(word: Optional[str], offer: Offer) -> Optional[bool]:
    """Товарный знак назван как знак товара, а не в обороте «для HP» / «совместим с HP»."""
    if not word:
        return None
    target = phonetic(word)
    if not target:
        return None
    hay = " . ".join([offer.title, offer.text] + [f"{c.name}: {c.value}" for c in offer.characteristics])
    if _COMPAT_RX.search(offer.title or ""):
        return False
    found_plain = False
    for toks in _segments(hay):
        for i, t in enumerate(toks):
            if t == target or (len(target) >= 5 and t.startswith(target)):
                prev = toks[i - 1] if i else ""
                if prev not in ("dlia", "for", "pod", "sovmestim", "sovmestimii", "analog"):
                    found_plain = True
    if found_plain:
        return True
    return _contains(hay, word) and not re.search(r"\b(для|for|под)\s+" + re.escape(word.lower()), hay.lower())


_CYR_HOMO = str.maketrans("авекмнорстху", "abekmhopctxy")
_MIXED = re.compile(r"(?=.*[а-яё])(?=.*[a-z0-9])")


def _code_token(t: str) -> str:
    """В обозначениях с цифрами и латиницей кириллица — двойники латинских букв: «СF259A» → «CF259A»."""
    return phonetic(t.translate(_CYR_HOMO) if _MIXED.match(t) else t)


def _segments(text: str) -> list[list[str]]:
    out = []
    for seg in re.split(r"[,;()\[\]|–—«»\"]+|\s-\s|\.\s", _DIM_SEP.sub("x", (text or "").lower())):
        toks = [_code_token(t) for t in re.findall(r"[a-zа-яё0-9]+", seg)]
        toks = [t for t in toks if t]
        if toks:
            out.append(toks)
    return out


def model_match(model: Optional[str], text: str) -> Optional[bool]:
    """True — модель из ТЗ названа без признаков другой модификации («D600 Pro», но не «D600 Pro 2 HS»);
    False — названа только другая модификация или не названа; None — в ТЗ нет модели."""
    if not model:
        return None
    # «59A / CF259A», «59A (CF259A)» — модель и артикул производителя: достаточно любого из обозначений
    alternatives = [a for a in re.split(r"\s*[/(),;]\s*|\s+или\s+", model) if phonetic(a)]
    if len(alternatives) > 1:
        results = [model_match(a, text) for a in alternatives]
        return True if any(results) else False
    target = phonetic(model)
    if not target:
        return None
    for toks in _segments(text):
        for i in range(len(toks)):
            joined, j = "", i
            for j in range(i, len(toks)):
                joined += toks[j]
                if len(joined) >= len(target):
                    break
            if joined != target:
                continue
            nxt = toks[j + 1] if j + 1 < len(toks) else ""
            if nxt and (nxt.isdigit() or nxt in _MOD_MARKS or re.fullmatch(r"(v|mk|gen|g)\d+", nxt)):
                continue
            return True
    return False


def _model_ok(model: Optional[str], offer: Offer) -> Optional[bool]:
    if not model:
        return None
    if model_match(model, offer.title or ""):
        return True
    hay = " . ".join([offer.text or ""] + [f"{c.name}: {c.value}" for c in offer.characteristics])
    return bool(model_match(model, hay))


# ---------------------------------------------------------------- итог по предложению

def match_offer(item: Item, offer: Offer, rules: Rules, llm_verdicts: Optional[dict[str, str]] = None) -> MatchResult:
    m = rules.matching
    identity = Identity(brand=_brand_ok(item.brand, offer), model=_model_ok(item.model, offer))
    drop = head_terms(item)
    checks: list[CharCheck] = []
    llm_used = False
    other_model = bool(item.model) and identity.model is False
    for req in item.requirements:
        check, used = check_requirement(req, offer, rules, (llm_verdicts or {}).get(req.id), drop, identity)
        if other_model and req.by_brand and not check.mandatory:
            # другая модель: то, что в ТЗ задавала модель, источник должен показать сам
            check = check.model_copy(update={"mandatory": True})
        checks.append(check)
        llm_used = llm_used or used
    reasons: list[str] = []
    fails = [c for c in checks if c.mandatory and c.status == CheckStatus.FAIL]
    missing = [c for c in checks if c.mandatory and c.status in (CheckStatus.MISSING, CheckStatus.UNCLEAR)]
    if checks:
        total = Decimal(0)
        got = Decimal(0)
        for c in checks:
            w = m.weight_mandatory if c.mandatory else m.weight_optional
            total += w
            credit = {CheckStatus.OK: Decimal(1), CheckStatus.ABOVE: m.above_credit,
                      CheckStatus.MISSING: Decimal(0) if c.mandatory else m.missing_optional_credit,
                      CheckStatus.UNCLEAR: Decimal(0) if c.mandatory else m.missing_optional_credit,
                      CheckStatus.FAIL: Decimal(0)}[c.status]
            got += w * credit
        score = float(got / total) if total else 0.0
    else:
        score = name_similarity(item.short_name or item.name, offer.title, m.token_synonyms)
    if fails:
        reasons += [f"не выполнено: {c.requirement} (в источнике: {c.offer_value or '—'})" for c in fails[:3]]
        return MatchResult(offer_id=offer.id, cls=MatchClass.UNSUITABLE, score=round(score, 3), checks=checks,
                           reasons=reasons, llm_used=llm_used)
    confirmed = [c for c in checks if c.mandatory and c.status in (CheckStatus.OK, CheckStatus.ABOVE)]
    soft = (m.one_unconfirmed_homogeneous and len(missing) == 1 and len(confirmed) >= 2
            and identity.brand is not False and identity.model is not False)
    if missing and not soft:
        if identity.model is False:
            reasons.append(f"модель не совпадает или не указана (в ТЗ — {item.model})")
        reasons += [f"обязательная характеристика не подтверждена источником: {c.requirement}" for c in missing[:3]]
        return MatchResult(offer_id=offer.id, cls=MatchClass.UNSUITABLE, score=round(score, 3), checks=checks,
                           reasons=reasons, llm_used=llm_used, not_confirmed=True)
    origin = {r.id for r in item.requirements if is_origin_requirement(r)}
    if other_model and not [c for c in confirmed if c.requirement_id not in origin]:
        # товар в ТЗ задан моделью: другая модель (картридж CF283X вместо CF259A) не взаимозаменяема, и признать
        # её однородной не по чему — кроме оригинальности, ни одна существенная характеристика не подтверждена
        reasons.append(f"другая модель или модель не указана (в ТЗ — {item.model}); существенных характеристик, "
                       f"по которым товар можно признать однородным, источник не подтверждает")
        return MatchResult(offer_id=offer.id, cls=MatchClass.UNSUITABLE, score=round(score, 3), checks=checks,
                           reasons=reasons, llm_used=llm_used)
    if score < float(m.min_score):
        reasons.append(f"мало совпадений с ТЗ: релевантность {round(score * 100)} % ниже порога {round(float(m.min_score) * 100)} %")
        return MatchResult(offer_id=offer.id, cls=MatchClass.UNSUITABLE, score=round(score, 3), checks=checks,
                           reasons=reasons, llm_used=llm_used)
    above = [c for c in checks if c.mandatory and c.status == CheckStatus.ABOVE]
    optional_fail = [c for c in checks if not c.mandatory and c.status == CheckStatus.FAIL]
    if missing:   # мягкое правило: одна существенная характеристика в источнике не указана
        reasons.append(f"в источнике не указано: {missing[0].requirement} (остальные существенные совпали)")
    if identity.brand is False:
        reasons.append(f"товарный знак не совпадает или не указан (в ТЗ — {item.brand})")
    if identity.model is False:
        reasons.append(f"модель не совпадает или не указана (в ТЗ — {item.model})")
    reasons += [f"выше требования: {c.requirement} (в источнике: {c.offer_value})" for c in above[:2]]
    notes: list[str] = []
    if item.model and identity.confirmed:
        # модель из ТЗ названа точно: её характеристики заданы производителем, расхождение второстепенного
        # значения на странице продавца — неточность описания, а не другой товар
        notes = [f"в описании продавца иначе: {c.requirement} (в источнике: {c.offer_value or '—'})"
                 for c in optional_fail[:2]]
    else:
        reasons += [f"отличается второстепенная характеристика: {c.requirement} (в источнике: {c.offer_value})"
                    for c in optional_fail[:2]]
    if not reasons:
        ok = sum(1 for c in checks if c.mandatory and c.status == CheckStatus.OK)
        what = f"все существенные характеристики совпали ({ok})" if ok else "совпадает описание товара"
        if item.brand:
            what += f", товарный знак {item.brand}"
        if item.model:
            what += f", модель {item.model}"
        return MatchResult(offer_id=offer.id, cls=MatchClass.IDENTICAL, score=round(score, 3), checks=checks,
                           reasons=[what] + notes, llm_used=llm_used)
    return MatchResult(offer_id=offer.id, cls=MatchClass.HOMOGENEOUS, score=round(score, 3), checks=checks,
                       reasons=reasons, llm_used=llm_used)
