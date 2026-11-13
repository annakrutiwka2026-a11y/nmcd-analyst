"""Шлюз обезличивания: перед отправкой текста во внешнюю модель убирает сведения о заказчике,
людях, адресах, реквизитах, суммах и датах. Технические характеристики остаются.

Таблица соответствий хранится только в памяти процесса и наружу не передаётся.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from app.config import CONFIG_DIR

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")),
    ("PHONE", re.compile(r"(?:\+7|8)[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")),
    ("URL", re.compile(r"https?://\S+")),
    ("ID", re.compile(r"(?:ИНН|КПП|ОГРН(?:ИП)?|БИК|ОКПО|р/с|к/с|л/с|ИКЗ)\s*[:№]?\s*\d{8,20}", re.I)),
    ("ID", re.compile(r"(?<!\d)\d{18,36}(?!\d)")),
    ("ADDR", re.compile(
        r"(?:\d{6},\s*)?(?:Российская Федерация,\s*)?(?:[А-ЯЁ][а-яё\-]+\s+(?:обл\.|область|край|респ\.|республика),\s*)?"
        r"(?:г\.|город|пос\.|с\.)\s*[А-ЯЁ][\w\-]+[^\n;]{0,120}?(?:д\.|дом)\s*\d+[^\n;,]{0,20}"
        r"(?:,\s*(?:корп\.|корпус|стр\.|строение|ком\.|комн\.|пом\.|оф\.|ауд\.)\s*[\w\-]+)*", re.I)),
    ("ADDR", re.compile(r"\d{6},\s*[^\n;]{5,140}")),
    ("MONEY", re.compile(r"\d[\d\s ]*(?:[.,]\d{1,2})?\s*(?:руб(?:лей|ля|\.)?|₽|рублей)", re.I)),
    ("PER", re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")),
    ("PER", re.compile(r"[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+")),
    ("DATE", re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")),
]

_LONG_ORG = re.compile(
    r"(?:Федеральное|Государственное|Муниципальное|Областное|Краевое)[^\n\"«]{0,200}?"
    r"(?:учреждение|предприятие|университет|институт|организация)[^\n\"«]{0,80}[\"«][^\"»\n]{3,200}[\"»]",
    re.I,
)


@dataclass
class Redaction:
    text: str
    counts: dict[str, int] = field(default_factory=dict)
    mapping: dict[str, str] = field(default_factory=dict)
    leaks: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        names = {"ORG": "организаций", "ADDR": "адресов", "PER": "персон", "PHONE": "телефонов", "EMAIL": "e-mail",
                 "ID": "реквизитов и номеров", "MONEY": "сумм", "DATE": "дат", "URL": "ссылок"}
        parts = [f"{names.get(k, k)}: {v}" for k, v in sorted(self.counts.items()) if v]
        return ", ".join(parts) if parts else "чувствительных сведений не найдено"


def _customer_terms() -> list[str]:
    path = CONFIG_DIR / "privacy.yaml"
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(t) for t in data.get("customer_terms", []) if str(t).strip()]


def redact(text: str, extra_terms: list[str] | None = None) -> Redaction:
    counts: dict[str, int] = {}
    mapping: dict[str, str] = {}
    seen: dict[str, str] = {}

    def sub(kind: str):
        def _f(m: re.Match[str]) -> str:
            original = m.group(0)
            if original in seen:
                return seen[original]
            counts[kind] = counts.get(kind, 0) + 1
            label = f"[{kind}_{counts[kind]}]"
            seen[original] = label
            mapping[label] = original
            return label
        return _f

    out = _LONG_ORG.sub(sub("ORG"), text)
    terms = sorted(set(_customer_terms() + (extra_terms or [])), key=len, reverse=True)
    for term in terms:
        out = re.sub(re.escape(term), sub("ORG"), out, flags=re.I)
    for kind, rx in _PATTERNS:
        out = rx.sub(sub(kind), out)
    leaks = [kind for kind, rx in _PATTERNS if kind in ("EMAIL", "PHONE") and rx.search(out)]
    return Redaction(text=out, counts=counts, mapping=mapping, leaks=leaks)
