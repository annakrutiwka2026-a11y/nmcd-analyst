"""Нормализация текста, термины для предварительного отбора и проверка цитат."""
from __future__ import annotations

import re

_STOP = {"для", "и", "в", "на", "с", "по", "из", "от", "до", "не", "или", "без", "при", "the", "of", "and", "товар",
         "товара", "поставка", "закупка", "шт", "штука", "белок", "человека"}


def norm_text(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[ \s]+", " ", s)


def tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-zа-я0-9]+", norm_text(s).replace("-", " ")) if t not in _STOP]


def terms(s: str) -> set[str]:
    toks = tokens(s)
    out = {t for t in toks if len(t) >= 4 or any(c.isdigit() for c in t)}
    out |= {f"{a} {b}" for a, b in zip(toks, toks[1:])}
    return out


def quote_found(quote: str, text: str) -> bool:
    """Цитата есть в тексте источника (без учёта регистра, пробелов и знаков препинания)."""
    q = re.sub(r"[^\wа-яё]+", " ", norm_text(quote)).strip()
    t = re.sub(r"[^\wа-яё]+", " ", norm_text(text))
    if not q:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) >= 4:
        return sum(1 for w in words if w in t) / len(words) >= 0.85
    return False
