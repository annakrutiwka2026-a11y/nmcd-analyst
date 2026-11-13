"""Обновление ряда ИПЦ из официального файла Росстата.

Источник: rosstat.gov.ru, раздел «Цены, инфляция», файл «Индексы потребительских цен на товары и услуги
по Российской Федерации» (лист «01», строки «к концу предыдущего месяца»).

    python tools/update_cpi.py                      # скачать актуальный файл и пересобрать data/cpi_rosstat.csv
    python tools/update_cpi.py --file data/rosstat/ipc_mes_09-2026.xlsx
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import ssl
import sys
import urllib.request
from datetime import date
from pathlib import Path

import certifi
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
PAGE = "https://rosstat.gov.ru/statistics/price"
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь"]


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    for name in ("russian_trusted_root_ca.pem", "russian_trusted_sub_ca.pem"):
        ctx.load_verify_locations(cafile=str(ROOT / "data" / "certs" / name))
    return ctx


def download() -> Path:
    req = urllib.request.Request(PAGE, headers={"User-Agent": "NMCD-Analyst/1.0"})
    html = urllib.request.urlopen(req, timeout=60, context=_ctx()).read().decode("utf-8", "replace")
    links = sorted(set(re.findall(r'href="(/storage/mediabank/ipc_mes_[^"]+\.xlsx)"', html)))
    if not links:
        raise SystemExit("На странице Росстата не найден файл ipc_mes_*.xlsx")
    url = "https://rosstat.gov.ru" + links[-1]
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "NMCD-Analyst/1.0"}),
                                  timeout=60, context=_ctx()).read()
    out = ROOT / "data" / "rosstat" / url.rsplit("/", 1)[1]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def convert(xlsx: Path, out_csv: Path) -> int:
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb["01"]
    years: dict[int, int] = {}
    rows = list(ws.iter_rows(values_only=True))
    for row in rows:
        if row and row[0] is None and any(isinstance(v, int) and 1990 < v < 2100 for v in row[1:]):
            years = {j: v for j, v in enumerate(row) if isinstance(v, int) and 1990 < v < 2100}
            break
    digest = hashlib.sha256(xlsx.read_bytes()).hexdigest()
    records = []
    for row in rows:
        label = str(row[0] or "").strip().lower()
        if label not in MONTHS:
            continue
        m = MONTHS.index(label) + 1
        for j, y in years.items():
            v = row[j] if j < len(row) else None
            if isinstance(v, (int, float)) and y >= 2015:
                pub_y, pub_m = (y + 1, 1) if m == 12 else (y, m + 1)
                records.append((f"{y:04d}-{m:02d}", f"{v:.2f}", date(pub_y, pub_m, 15).isoformat(),
                                f"Росстат, {xlsx.name}, лист 01, SHA-256 {digest[:16]}"))
        if len(records) > 0 and label == "декабрь":
            break
    records.sort()
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "index_mom", "published", "source"])
        w.writerows(records)
    return len(records)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "cpi_rosstat.csv")
    args = ap.parse_args()
    xlsx = args.file or download()
    n = convert(xlsx, args.out)
    print(f"{args.out}: месяцев {n} (из {xlsx.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
