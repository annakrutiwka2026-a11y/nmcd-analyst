"""Форматирование чисел, денег и дат для отчётов. Сумма прописью — для документа обоснования."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

MSK = timezone(timedelta(hours=3))


def money(v: Optional[Decimal], unit: str = " ₽") -> str:
    if v is None:
        return "—"
    q = Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{q:.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return f"{sign}{' '.join(reversed(groups))},{frac}{unit}"


def num(v: Optional[Decimal], digits: int = 2) -> str:
    if v is None:
        return "—"
    q = Decimal(v).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    s = f"{q:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s


def pct(v: Optional[Decimal], digits: int = 2) -> str:
    if v is None:
        return "—"
    return num(Decimal(v) * 100, digits) + " %"


def dt(v: Optional[datetime | date], with_time: bool = False) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        v = v.astimezone(MSK)
        return v.strftime("%d.%m.%Y %H:%M МСК") if with_time else v.strftime("%d.%m.%Y")
    return v.strftime("%d.%m.%Y")


_ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_SCALES = [("", "", "", False), ("тысяча", "тысячи", "тысяч", True), ("миллион", "миллиона", "миллионов", False),
           ("миллиард", "миллиарда", "миллиардов", False)]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _triad(n: int, female: bool) -> list[str]:
    words = []
    h, rest = divmod(n, 100)
    if h:
        words.append(_HUNDREDS[h])
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        t, o = divmod(rest, 10)
        if t:
            words.append(_TENS[t])
        if o:
            words.append((_ONES_F if female else _ONES)[o])
    return words


def rubles_in_words(v: Decimal) -> str:
    """Сумма прописью: «один миллион двести тысяч рублей 50 копеек»."""
    q = Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rub = int(q)
    kop = int((q - rub) * 100)
    if rub == 0:
        words = ["ноль"]
    else:
        words = []
        n = rub
        parts = []
        i = 0
        while n > 0:
            n, tri = divmod(n, 1000)
            parts.append((i, tri))
            i += 1
        for i, tri in reversed(parts):
            if tri == 0:
                continue
            one, few, many, female = _SCALES[i] if i < len(_SCALES) else ("", "", "", False)
            words += _triad(tri, female)
            if one:
                words.append(_plural(tri, one, few, many))
    text = " ".join(words)
    text = text[0].upper() + text[1:]
    return f"{text} {_plural(rub, 'рубль', 'рубля', 'рублей')} {kop:02d} {_plural(kop, 'копейка', 'копейки', 'копеек')}"
