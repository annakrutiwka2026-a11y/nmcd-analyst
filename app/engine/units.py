"""Единицы измерения, числа и количества: канонический вид, перевод, разбор текста.

Модуль не знает о закупках: он отвечает на вопросы «что это за единица», «сколько это в других
единицах» и «какие числа с единицами записаны в строке».
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from app.models import Quantity

NBSP = " "

# (каноническая запись, размерность, множитель к основной единице размерности, варианты записи через «|»)
_SPEC: tuple[tuple[str, str, str, str], ...] = (
    # масса, основа — грамм
    ("нг", "mass", "1e-9", "нг|ng|нанограмм|нанограмма|нанограммов"),
    ("мкг", "mass", "1e-6", "мкг|μg|ug|mcg|мкгр|микрограмм|микрограмма|микрограммов"),
    ("мг", "mass", "1e-3", "мг|mg|миллиграмм|миллиграмма|миллиграммов"),
    ("г", "mass", "1", "г|g|гр|грамм|грамма|граммов|gram|grams"),
    ("кг", "mass", "1e3", "кг|kg|килограмм|килограмма|килограммов"),
    ("т", "mass", "1e6", "т|тонна|тонны|тонн"),
    # объём, основа — миллилитр
    ("мкл", "volume", "1e-3", "мкл|μl|ul|микролитр|микролитра|микролитров"),
    ("мл", "volume", "1", "мл|ml|миллилитр|миллилитра|миллилитров|см3|cm3|куб см|см куб"),
    ("л", "volume", "1e3", "л|l|литр|литра|литров|дм3|л;дм3"),
    ("м³", "volume", "1e6", "м3|m3|куб м|м куб|кубический метр|кубических метров|кубометр|кубометра|кубометров"),
    # длина, основа — миллиметр
    ("нм", "length", "1e-6", "нм|nm|нанометр|нанометра|нанометров"),
    ("мкм", "length", "1e-3", "мкм|μm|um|микрон|микрона|микронов|микрометр|микрометра|микрометров"),
    ("мм", "length", "1", "мм|mm|миллиметр|миллиметра|миллиметров"),
    ("см", "length", "10", "см|cm|сантиметр|сантиметра|сантиметров"),
    ("дм", "length", "100", "дм|dm|дециметр|дециметра|дециметров"),
    ("м", "length", "1e3", "м|m|метр|метра|метров|пог м|погонный метр|погонных метров"),
    ("км", "length", "1e6", "км|km|километр|километра|километров"),
    ("дюйм", "length", "25.4", "дюйм|дюйма|дюймов|inch|inches|\"|″|′′|''"),
    # площадь, основа — квадратный метр
    ("мм²", "area", "1e-6", "мм2|mm2|кв мм|мм кв"),
    ("см²", "area", "1e-4", "см2|cm2|кв см|см кв"),
    ("м²", "area", "1", "м2|m2|кв м|м кв|квадратный метр|квадратного метра|квадратных метров|кв метр"),
    # счётные единицы
    ("шт", "count", "1", "шт|штука|штуки|штук|штуку|штуке|pcs|pc|ед|единица|единицы|единиц|усл ед|"
                         "условная единица|условных единиц"),
    ("тыс. шт", "count", "1e3", "тыс шт|тысяча штук|тысяч штук"),
    ("лист", "sheet", "1", "лист|листа|листов|листы|листе|листах|sheet|sheets"),
    # тара: между собой не переводится
    ("пачка", "container", "1", "пачка|пачки|пачку|пачке|пачек|пачкой|пачках|пач"),
    ("упаковка", "container", "1", "упаковка|упаковки|упаковку|упаковке|упаковок|упаковкой|упаковках|уп|упак|"
                                   "pack|package|pkg"),
    ("коробка", "container", "1", "коробка|коробки|коробку|коробке|коробок|коробкой|коробках|кор|короб|box"),
    ("флакон", "container", "1", "флакон|флакона|флаконов|флаконе|флаконы|фл|флак|vial|vials"),
    ("набор", "container", "1", "набор|набора|наборов|наборе|наборы|наб|kit|set"),
    ("комплект", "container", "1", "комплект|комплекта|комплектов|комплекте|комплекты|компл|к-т|кт"),
    ("рулон", "container", "1", "рулон|рулона|рулонов|рулоне|рулоны|рул|roll"),
    ("бутылка", "container", "1", "бутылка|бутылки|бутылку|бутылке|бутылок|бут|бутыль|бутыли|бутылей"),
    ("банка", "container", "1", "банка|банки|банку|банке|банок|бан"),
    ("ампула", "container", "1", "ампула|ампулы|ампулу|ампуле|ампул|амп"),
    ("туба", "container", "1", "туба|тубы|тубу|тубе|туб|тюбик|тюбика|тюбиков"),
    ("мешок", "container", "1", "мешок|мешка|мешков|мешке|мешки|меш"),
    ("канистра", "container", "1", "канистра|канистры|канистру|канистре|канистр|кан"),
    ("ящик", "container", "1", "ящик|ящика|ящиков|ящике|ящики|ящ"),
    ("блок", "container", "1", "блок|блока|блоков|блоке|блоки"),
    ("паллета", "container", "1", "паллета|паллеты|паллету|паллете|паллет|палета|палеты|палет|поддон|поддона|"
                                  "поддонов|пал"),
    ("пара", "container", "1", "пара|пары|пару|паре|пар"),
    ("пакет", "container", "1", "пакет|пакета|пакетов|пакете|пакеты|пак"),
    ("катушка", "container", "1", "катушка|катушки|катушку|катушке|катушек|кат|бобина|бобины|бобин|боб"),
    # производные величины
    ("г/м²", "area_density", "1", "г/м2|g/m2|gsm|г/кв м|гр/м2|гр/кв м|г/м кв|г на м2"),
    ("г/см³", "density", "1e3", "г/см3|g/cm3|г/куб см"),
    ("кг/м³", "density", "1", "кг/м3|kg/m3|кг/куб м"),
    ("°C", "temperature", "1", "°c|°с|0c|0с|oc|ос|̊c|̊с|град|градус|градуса|градусов|град c|град с|"
                               "градус цельсия|градуса цельсия|градусов цельсия|град цельсия|deg c|c°|с°"),
    ("К", "temperature", "1", "кельвин|кельвина|кельвинов"),
    ("мВт", "power", "1e-3", "мвт|милливатт|милливатта"),
    ("Вт", "power", "1", "вт|w|ватт|ватта"),
    ("кВт", "power", "1e3", "квт|kw|киловатт|киловатта"),
    ("МВт", "power", "1e6", "мегаватт|мегаватта"),
    ("В", "voltage", "1", "в|v|вольт|вольта"),
    ("кВ", "voltage", "1e3", "кв|kv|киловольт|киловольта"),
    ("мА", "current", "1e-3", "ма|ma|миллиампер|миллиампера"),
    ("А", "current", "1", "а|a|ампер|ампера"),
    ("Гц", "frequency", "1", "гц|hz|герц|герца"),
    ("кГц", "frequency", "1e3", "кгц|khz|килогерц"),
    ("МГц", "frequency", "1e6", "мгц|mhz|мегагерц"),
    ("ГГц", "frequency", "1e9", "ггц|ghz|гигагерц"),
    ("мм/с", "speed", "1", "мм/с|мм/сек|mm/s"),
    ("см/с", "speed", "10", "см/с|см/сек|cm/s"),
    ("м/с", "speed", "1e3", "м/с|м/сек|m/s"),
    ("мм/мин", "speed", "1/60", "мм/мин|mm/min"),
    ("м/мин", "speed", "1000/60", "м/мин|m/min"),
    ("км/ч", "speed", "1000000/3600", "км/ч|км/час|km/h"),
    ("об/мин", "rotation", "1", "об/мин|оборотов в минуту|rpm"),
    ("%", "percent", "1", "%|процент|процента|процентов|проц|percent|pct"),
    ("мс", "time", "1e-3", "мс|ms|миллисекунда|миллисекунды|миллисекунд"),
    ("с", "time", "1", "с|сек|s|sec|секунда|секунды|секунд"),
    ("мин", "time", "60", "мин|min|минута|минуты|минут"),
    ("ч", "time", "3600", "ч|час|часа|часов|h|hr|hrs"),
    ("сут", "time", "86400", "сут|сутки|суток|день|дня|дней|дн|day|days|календарных дней"),
    ("нед", "time", "604800", "нед|неделя|недели|недель|week|weeks"),
    ("мес", "calendar", "1", "мес|месяц|месяца|месяцев|month|months"),
    ("год", "calendar", "12", "год|года|лет|year|years"),
    ("КБ", "data", "1024", "кб|kb|килобайт|килобайта"),
    ("МБ", "data", "1048576", "мб|mb|мегабайт|мегабайта"),
    ("ГБ", "data", "1073741824", "гб|gb|гигабайт|гигабайта"),
    ("ТБ", "data", "1099511627776", "тб|tb|терабайт|терабайта"),
    ("Па", "pressure", "1", "па|pa|паскаль|паскаля"),
    ("кПа", "pressure", "1e3", "кпа|kpa|килопаскаль"),
    ("МПа", "pressure", "1e6", "мпа|mpa|мегапаскаль"),
    ("бар", "pressure", "1e5", "бар|bar"),
    ("нг/мл", "concentration", "1", "нг/мл|ng/ml"),
    ("мкг/мл", "concentration", "1e3", "мкг/мл|μg/ml|ug/ml|mcg/ml|мг/л|mg/l"),
    ("мг/мл", "concentration", "1e6", "мг/мл|mg/ml|г/л|g/l"),
    ("Да", "molar_mass", "1", "дальтон|da"),
    ("кДа", "molar_mass", "1e3", "кда|kda|килодальтон"),
    ("дБ", "sound", "1", "дб|db|дба|dba|децибел"),
    ("лм", "luminous_flux", "1", "лм|lm|люмен"),
    ("лк", "illuminance", "1", "лк|lx|люкс"),
    ("кд/м²", "luminance", "1", "кд/м2|cd/m2|нит|nit|nits"),
    ("dpi", "resolution", "1", "dpi|точек на дюйм|т/д"),
    ("пикс", "pixel", "1", "пикс|px|пиксель|пикселя|пикселей"),
    ("А·ч", "charge", "1", "ач|ah|ампер-час"),
    ("мА·ч", "charge", "1e-3", "мач|mah"),
    ("°", "angle", "1", "°"),
)

# Записи, где регистр меняет смысл.
_CASED: dict[str, str] = {
    "мВт": "мВт", "МВт": "МВт", "mW": "мВт", "MW": "МВт", "МПа": "МПа", "MPa": "МПа",
    "К": "К", "K": "К", "С": "°C", "C": "°C", "Да": "Да",
}
_OFFSET: dict[str, Fraction] = {"К": Fraction("-273.15")}
_PIECE_DIMS = ("count", "container")
_SCAN_WORDS = frozenset("ваикоуa")   # однобуквенные предлоги и союзы: в тексте это не единицы
_MINUS_BEFORE = re.compile(r"(?:выше|ниже|до|от|при|менее|более|около|минус|не|темп\w*|хран\w*)\s*$")


def _clean(text: str) -> str:
    """Совместимая нормализация Юникода: «²» → «2», неразрывный пробел → пробел, «µ» → «μ»."""
    return unicodedata.normalize("NFKC", text)


def _key(unit: str) -> str:
    return re.sub(r"[\s.^*·]+", "", _clean(unit).casefold())


def _build() -> tuple[dict[str, tuple[str, Fraction]], dict[str, str]]:
    units: dict[str, tuple[str, Fraction]] = {}
    aliases: dict[str, str] = {}
    for canon, dim, factor, variants in _SPEC:
        units[canon] = (dim, Fraction(factor))
        for variant in variants.split("|"):
            key = _key(variant)
            if aliases.setdefault(key, canon) != canon:
                raise ValueError(f"Запись единицы «{variant}» относится сразу к «{aliases[key]}» и «{canon}»")
    return units, aliases


_UNITS, _ALIASES = _build()


def _resolve(raw: str, *, scanning: bool = False, prefer: str | None = None, next_char: str = "") -> str | None:
    bare = raw.strip().strip("()[]").strip().rstrip(".").strip()
    if not bare:
        return None
    if bare in _CASED:
        return _CASED[bare]
    key = _key(bare)
    if not key:
        return None
    if scanning and len(key) == 1 and bare.islower():
        if key in _SCAN_WORDS or (key == "с" and next_char.isalpha()):
            return None
    if key == "л" and prefer and dimension(prefer) == "sheet":
        return "лист"
    found = _ALIASES.get(key)
    if found is not None or scanning:
        return found
    return key


def canon_unit(u: str | None) -> str | None:
    """Каноническая запись единицы: «μg» → «мкг», «пач.» → «пачка», «г/кв.м» → «г/м²», «0C» → «°C».

    Незнакомая единица возвращается в нижнем регистре без пробелов и точек, чтобы её можно было сравнивать.
    """
    if u is None:
        return None
    return _resolve(str(u))


def dimension(u: str | None) -> str | None:
    """Размерность единицы: mass, volume, length, count, sheet, container (тара) и другие; None — неизвестна."""
    canon = canon_unit(u)
    spec = _UNITS.get(canon) if canon else None
    return spec[0] if spec else None


def _to_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def convert(value: Decimal, from_unit: str, to_unit: str) -> Decimal | None:
    """Перевод значения между единицами одной размерности; None — перевести нельзя.

    Штуки и листы считаются одной счётной величиной (1 : 1). Разные виды тары не переводятся.
    """
    a, b = canon_unit(from_unit), canon_unit(to_unit)
    if a is None or b is None:
        return None
    if a == b:
        return Decimal(value)
    ua, ub = _UNITS.get(a), _UNITS.get(b)
    if ua is None or ub is None:
        return None
    if ua[0] != ub[0] and {ua[0], ub[0]} != {"count", "sheet"}:
        return None
    if ua[0] == "container":
        return None
    base = Fraction(Decimal(value)) * ua[1] + _OFFSET.get(a, Fraction(0))
    return _to_decimal((base - _OFFSET.get(b, Fraction(0))) / ub[1])


# ---------------------------------------------------------------- числа

_GROUPED = r"\d{1,3}(?: \d{3})+(?!\d)"
_NUM_BODY = rf"(?:\d{{1,3}}(?:,\d{{3}})+\.\d+|\d{{1,3}}(?:\.\d{{3}})+,\d+|(?:{_GROUPED}|\d+)(?:[.,]\d+)?)"
_NUM_FULL = re.compile(rf"^(?P<sign>[+\-−–—])?\s*(?P<num>{_NUM_BODY})$")
_NUM_SCAN = re.compile(rf"(?<![^\W_]){_NUM_BODY}")
_DASHES = "-−–—"


def _number(token: str) -> Decimal:
    t = token.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", t):
        t = t.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", t):
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    return Decimal(t)


def parse_number(text: str) -> Decimal | None:
    """Число, записанное в строке целиком: «0,05», «1 600», «2.5», «−20». Иначе None."""
    if text is None:
        return None
    s = _clean(str(text)).strip().lstrip("≈~").strip()
    m = _NUM_FULL.match(s)
    if not m:
        return None
    value = _number(m["num"])
    return -value if m["sign"] and m["sign"] != "+" else value


@dataclass(frozen=True)
class Found:
    """Число из текста: значение, распознанная единица и границы в нормализованной строке."""

    value: Decimal
    unit: str | None
    start: int
    end: int


def _unit_at(s: str, pos: int, prefer: str | None, value: Decimal) -> tuple[str | None, int]:
    lead = pos
    while lead < len(s) and s[lead] == " " and lead - pos < 2:
        lead += 1
    window = s[lead:lead + 24]
    cut = window.find("\n")
    if cut >= 0:
        window = window[:cut]
    for size in range(len(window), 0, -1):
        piece = window[:size]
        last = piece[-1]
        nxt = s[lead + size] if lead + size < len(s) else ""
        if last.isspace() or (last.isalpha() and nxt.isalnum()) or (last.isdigit() and nxt.isdigit()):
            continue
        after = s[lead + size:].lstrip(" ")[:1]
        unit = _resolve(piece, scanning=True, prefer=prefer, next_char=after)
        if unit is None:
            continue
        if unit == "г" and value == value.to_integral_value() and 1900 <= value <= 2100:
            return None, pos      # «2026 г.» — год, а не граммы
        if unit == "К" and value < 1000:
            return None, pos      # «4K» — разрешение экрана, а не кельвины
        return unit, lead + size
    return None, pos


def scan_quantities(text: str, prefer: str | None = None) -> tuple[str, list[Found]]:
    """Все отдельно стоящие числа в тексте с единицами. Возвращает нормализованную строку и находки.

    Числа внутри обозначений («А4», «IL-15», «D600», «600х600х600») пропускаются.
    `prefer` — единица, в пользу которой толкуется «л.»: при «лист» это листы, иначе литры.
    """
    s = _clean(text or "")
    found: list[Found] = []
    consumed = 0
    for m in _NUM_SCAN.finditer(s):
        i, j = m.span()
        if i < consumed:
            continue
        prev = s[i - 1] if i else ""
        if prev == "_":
            continue
        before = s[i - 2] if i >= 2 else ""
        if prev and prev in ".,×*" and before.isdigit():
            continue
        nxt = s[j:j + 2]
        if len(nxt) == 2 and nxt[0] in "xх×*.," and nxt[1].isdigit():
            continue
        negative, start = False, i
        if prev and prev in _DASHES:
            if before.isalpha():
                continue
            if not before.isdigit():
                k = i - 2
                while k >= 0 and s[k].isspace():
                    k -= 1
                if not before.isspace() or k < 0 or not (s[k].isdigit() or s[k] in "%°"):
                    negative, start = True, i - 1
        elif prev == " " and before and before in _DASHES:
            head = s[:i - 2]
            if not head.strip() or _MINUS_BEFORE.search(head.casefold()):
                negative, start = True, i - 2
        value = _number(m.group(0))
        unit, end = _unit_at(s, j, prefer, value)
        if unit is None and s[j:j + 1].isalpha():
            continue      # «3D», «4K» — часть обозначения
        found.append(Found(-value if negative else value, unit, start, end))
        consumed = end
    return s, found


def parse_quantity(text: str, prefer: str | None = None) -> Quantity | None:
    """Первое число с распознанной единицей: «25 мкг», «500 листов», «500 л.».

    Если задана `prefer`, сначала ищется количество той же размерности («л.» при prefer="лист" — листы).
    """
    _, found = scan_quantities(text, prefer)
    with_unit = [f for f in found if f.unit is not None]
    if not with_unit:
        return None
    want = dimension(prefer) if prefer else None
    if want:
        for f in with_unit:
            if dimension(f.unit) == want:
                return Quantity(value=f.value, unit=f.unit)
    return Quantity(value=with_unit[0].value, unit=with_unit[0].unit)


# ---------------------------------------------------------------- вложенная тара

@dataclass(frozen=True)
class Multipack:
    """Запись вида «коробка 5 пачек по 500 л.»: сколько внутренних единиц во внешней."""

    count: Decimal
    inner: str
    outer: str | None = None
    each: Quantity | None = None


def _piece_pattern() -> str:
    keys = sorted((k for k, canon in _ALIASES.items() if _UNITS[canon][0] in _PIECE_DIMS), key=len, reverse=True)
    return r"(?<![^\W\d_])(?:" + "|".join(re.escape(k) for k in keys) + r")(?![^\W\d_])\.?"


_PIECE = _piece_pattern()
_PER = rf"(?P<per>{_NUM_BODY}\s?[^\s\d,;()]{{0,14}})"
_MP_OUTER = re.compile(rf"(?P<outer>{_PIECE})\s*[(\[:,=–—-]?\s*(?:по|из|содержит)?\s*(?P<n>\d{{1,6}})\s*(?P<inner>{_PIECE})")
_MP_INNER = re.compile(rf"(?P<n>\d{{1,6}})\s*(?P<inner>{_PIECE})\s*(?:/|в|на)\s*(?:1\s+|одной\s+|одном\s+)?(?P<outer>{_PIECE})")
_MP_EACH = re.compile(rf"(?P<n>\d{{1,6}})\s*(?P<inner>{_PIECE})\s*(?:по|[xх×*])\s*{_PER}")
_MP_TIMES = re.compile(rf"(?<![\d.,xх×*])(?P<n>\d{{1,4}})\s*[xх×*]\s*{_PER}(?!\s*[xх×*]\s*\d)")
_MP_TAIL = re.compile(rf"\s*(?:по|[xх×*])\s*{_PER}")


def find_multipack(text: str, prefer: str | None = None) -> Multipack | None:
    """Вложенная тара в тексте: «коробка 5 пачек», «5 пач. по 500 л.», «10 шт/уп», «5х500 л»."""
    s = _clean(text or "").casefold()
    best: tuple[int, int, re.Match[str]] | None = None
    for order, pattern in enumerate((_MP_OUTER, _MP_INNER, _MP_EACH, _MP_TIMES)):
        for m in pattern.finditer(s):
            if order == 3:
                each = parse_quantity(m["per"], prefer)
                if each is None or dimension(each.unit) not in ("sheet", "mass", "volume"):
                    continue
            if best is None or (m.start(), order) < (best[0], best[1]):
                best = (m.start(), order, m)
            break
    if best is None:
        return None
    _, order, m = best
    groups = m.groupdict()
    count = Decimal(groups["n"])
    if count <= 0:
        return None
    inner = canon_unit(groups["inner"]) if groups.get("inner") else "шт"
    outer = canon_unit(groups["outer"]) if groups.get("outer") else None
    per = groups.get("per")
    if per is None:
        tail = _MP_TAIL.match(s, m.end())
        per = tail["per"] if tail else None
    each = parse_quantity(per, prefer) if per else None
    return Multipack(count=count, inner=inner or "шт", outer=outer, each=each)


def parse_multipack(text: str) -> tuple[Decimal, str] | None:
    """«коробка 5 пачек», «5 пач. по 500 л.» → (5, «пачка»). Нет вложенной тары — None."""
    found = find_multipack(text)
    return (found.count, found.inner) if found else None


# ---------------------------------------------------------------- запись чисел в пояснениях

def fmt_num(value: Decimal | int | str, places: int | None = None) -> str:
    """Число по-русски: неразрывный пробел между тысячами, запятая перед дробью."""
    d = Decimal(value)
    if places is not None:
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{d:f}"
    if places is None and "." in text:
        text = text.rstrip("0").rstrip(".")
    negative = text.startswith("-")
    whole, _, frac = text.lstrip("-").partition(".")
    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    out = NBSP.join(groups) + ("," + frac if frac else "")
    return ("−" if negative and Decimal(text) != 0 else "") + out


def fmt_money(value: Decimal | int | str) -> str:
    """Сумма в рублях: «129 000 ₽», «47 333,33 ₽»."""
    d = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return fmt_num(d, None if d == d.to_integral_value() else 2) + NBSP + "₽"


def fmt_pct(share: Decimal | int | str, places: int = 2) -> str:
    """Доля в процентах: 0.3445 → «34,45 %»."""
    return fmt_num(Decimal(share) * 100, places) + NBSP + "%"


def fmt_qty(value: Decimal | int | str, unit: str | None) -> str:
    """Количество с единицей: «500 лист», «10 мкг»."""
    return fmt_num(value) + (NBSP + unit if unit else "")


_AREA_HINT = re.compile(r"площад|1\s*м\s*2|1\s*м²|кв\.?\s*м|на\s*м2|плотн|граммаж", re.I)
_VALUE_UNIT = re.compile(r"^(.*?\d)\s*([^\d\s≥≤<>=±~][^\d≥≤<>=±~]*)$")


def split_value_unit(name: str, value: str) -> tuple[str, str | None]:
    """Значение характеристики реестра без единицы и каноническая единица.

    «80Грамм» → («80», «г»); «≥ 160 и < 180 Грамм» → («≥ 160 и < 180», «г»). У характеристики
    «Масса бумаги площадью 1м2» граммы означают г/м². Текст без числа и единицы возвращается как есть.
    """
    v = " ".join((value or "").split())
    m = _VALUE_UNIT.match(v)
    if not m:
        return v, None
    head, tail = m.group(1).strip(), m.group(2).strip().rstrip(".")
    unit = _resolve(tail)
    if unit is None or dimension(unit) is None:
        return v, None
    if unit == "г" and _AREA_HINT.search(name or ""):
        unit = "г/м²"
    return head, unit
