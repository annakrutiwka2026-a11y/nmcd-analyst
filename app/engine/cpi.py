"""Индекс потребительских цен Росстата: приведение цен прошлых периодов к уровню на дату расчёта."""
from __future__ import annotations

import calendar
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Iterable

from app.engine.rules import ROOT
from app.engine.units import fmt_num

DEFAULT_CPI_PATH = ROOT / "data" / "cpi_rosstat.csv"
MSK = timezone(timedelta(hours=3))
_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
           "декабрь")
_K_QUANTUM = Decimal("0.000000001")

Month = tuple[int, int]


def as_date(value: date | datetime) -> date:
    """Календарный день по московскому времени (для даты без времени — она сама)."""
    if isinstance(value, datetime):
        return value.astimezone(MSK).date() if value.tzinfo else value.date()
    return value


def add_months(day: date, months: int) -> date:
    """Дата через заданное число календарных месяцев (31-е число сдвигается на конец месяца)."""
    year, month0 = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month0 + 1, min(day.day, calendar.monthrange(year, month0 + 1)[1]))


def older_than(price_date: date | datetime, calc_date: date | datetime, months: int) -> bool:
    """Цена старше заданного числа месяцев на дату расчёта (ровно N месяцев — ещё не старше)."""
    return add_months(as_date(price_date), months) < as_date(calc_date)


def _shift(month: Month, by: int) -> Month:
    year, month0 = divmod(month[0] * 12 + month[1] - 1 + by, 12)
    return year, month0 + 1


def _span(first: Month, last: Month) -> list[Month]:
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = _shift(cur, 1)
    return out


def month_name(month: Month) -> str:
    return f"{_MONTHS[month[1] - 1]} {month[0]}"


class CpiSeries:
    """Ряд ИПЦ «месяц к предыдущему месяцу», в процентах (100,50 — рост на 0,5 %)."""

    def __init__(self, rows: Iterable[tuple[str, Decimal | str, date | str | None]], source: str = ""):
        self.source = source
        self._index: dict[Month, Decimal] = {}
        self._published: dict[Month, date | None] = {}
        for month, index, published in rows:
            year, _, mon = str(month).strip().partition("-")
            key = (int(year), int(mon))
            if not 1 <= key[1] <= 12:
                raise ValueError(f"Неверный месяц в ряду ИПЦ: {month}")
            value = Decimal(str(index).strip().replace(",", "."))
            if value <= 0:
                raise ValueError(f"Неверное значение ИПЦ за {month}: {index}")
            self._index[key] = value
            if isinstance(published, str):
                published = date.fromisoformat(published.strip()) if published.strip() else None
            self._published[key] = published

    def __len__(self) -> int:
        return len(self._index)

    @property
    def first_month(self) -> str | None:
        return "%04d-%02d" % min(self._index) if self._index else None

    @property
    def last_month(self) -> str | None:
        return "%04d-%02d" % max(self._index) if self._index else None

    @classmethod
    def load(cls, path: str | None = None) -> "CpiSeries | None":
        """Читает CSV: month (ГГГГ-ММ), index_mom, published (ГГГГ-ММ-ДД), source. Нет файла — None.

        Строки с нечитаемыми значениями пропускаются: пропуск месяца обнаружится при расчёте коэффициента.
        """
        target = Path(path) if path else DEFAULT_CPI_PATH
        if not target.is_file():
            return None
        rows: list[tuple[str, Decimal, date | None]] = []
        source = ""
        with target.open(encoding="utf-8-sig", newline="") as fh:
            head = fh.readline()
            fh.seek(0)
            reader = csv.DictReader(fh, delimiter=";" if head.count(";") > head.count(",") else ",")
            for row in reader:
                try:
                    month = (row.get("month") or "").strip()
                    year, _, mon = month.partition("-")
                    if not (year.isdigit() and mon.isdigit() and 1 <= int(mon) <= 12):
                        continue
                    index = Decimal((row.get("index_mom") or "").strip().replace(",", "."))
                    published = (row.get("published") or "").strip()
                    day = date.fromisoformat(published) if published else None
                except (InvalidOperation, ValueError):
                    continue
                if index <= 0:
                    continue
                rows.append((month, index, day))
                source = (row.get("source") or "").strip() or source
        return cls(rows, source=source)

    def factor(self, price_date: date | datetime, calc_date: date | datetime, threshold_months: int,
               formula: str = "chain", from_price_month: bool = False,
               max_lag_months: int = 3) -> tuple[Decimal | None, str]:
        """Коэффициент пересчёта цены к уровню на дату расчёта и пояснение.

        Берутся месяцы после месяца цены и до последнего месяца, опубликованного к дате расчёта (месяц расчёта
        не входит). `formula="chain"` — произведение месячных индексов, `"sum"` — сумма месячных приростов.
        None — ряд не покрывает период (пропуск месяца или ряд давно не обновлялся).
        """
        price_day, calc_day = as_date(price_date), as_date(calc_date)
        if not older_than(price_day, calc_day, threshold_months):
            return Decimal(1), f"цена не старше {threshold_months} мес. на дату расчёта, индексация не нужна"
        price_month, calc_month = (price_day.year, price_day.month), (calc_day.year, calc_day.month)
        first = price_month if from_price_month else _shift(price_month, 1)
        usable = {m for m, day in self._published.items() if m < calc_month and (day is None or day <= calc_day)}
        if not usable:
            return None, "в ряду ИПЦ нет значений, опубликованных к дате расчёта"
        last = max(usable)
        lag = (calc_month[0] - last[0]) * 12 + calc_month[1] - last[1]
        if lag > max_lag_months:
            return None, (f"ряд ИПЦ заканчивается на {month_name(last)}: до месяца расчёта больше "
                          f"{max_lag_months} мес., ряд нужно обновить")
        if last < first:
            return Decimal(1), "после месяца цены ещё не опубликовано ни одного значения ИПЦ"
        months = _span(first, last)
        missing = [m for m in months if m not in usable]
        if missing:
            names = ", ".join(month_name(m) for m in missing[:4]) + (" и др." if len(missing) > 4 else "")
            return None, f"в ряду ИПЦ нет значений за {names}"
        with localcontext() as ctx:
            ctx.prec = 28
            if formula == "sum":
                k = (Decimal(100) + sum((self._index[m] - 100 for m in months), Decimal(0))) / 100
            else:
                k = Decimal(1)
                for m in months:
                    k *= self._index[m] / 100
            k = k.quantize(_K_QUANTUM, rounding=ROUND_HALF_UP)
        how = "сумма месячных приростов" if formula == "sum" else "произведение месячных индексов"
        period = month_name(first) if first == last else f"{month_name(first)} – {month_name(last)}"
        return k, (f"ИПЦ Росстата (месяц к предыдущему), {how} за {period}, {len(months)} мес.: "
                   f"K = {fmt_num(k)}")
