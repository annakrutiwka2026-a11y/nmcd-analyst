"""Командная строка: расчёт НМЦД по файлу ТЗ без веб-интерфейса.

    python -m app.cli analyze examples/tz/tz_paper.docx --out out/paper
    python -m app.cli analyze описание.txt --no-llm --no-web
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.ingest.reader import read_input
from app.models import CalcStatus, TraceEvent
from app.pipeline import Run
from app.report.fmt import MSK, money
from app.report.render import save_all


def _print_event(ev: TraceEvent) -> None:
    print(f"{ev.ts.astimezone(MSK).strftime('%H:%M:%S')}  {ev.message}", flush=True)


async def analyze(path: Path, out: Path | None, use_llm: bool, use_web: bool, quiet: bool) -> int:
    settings = get_settings()
    doc = read_input(path.read_bytes(), path.name)
    run = Run(settings, progress=None if quiet else _print_event, use_llm=use_llm, use_web=use_web)
    report = await run.run(doc)
    report.settings["org_name"] = settings.org_name
    target = out or (settings.data_dir / "runs" / report.id)
    paths = save_all(report, target, settings.org_name)
    print()
    print(f"Закупка: {report.procurement.title}")
    for r in report.items:
        c = r.calc
        status = "обоснована" if c and c.status == CalcStatus.FINAL else "недостаточно данных"
        print(f"  {r.item.number}. {r.item.name[:70]} — {status}; цена за ед. {money(c.unit_price) if c else '—'}; "
              f"НМЦД {money(c.total) if c else '—'}")
    print(f"НМЦД по обоснованным позициям: {money(report.total)}")
    for kind, p in paths.items():
        print(f"  {kind}: {p}")
    return 0 if report.status == "done" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nmcd", description="Обоснование НМЦД методом сопоставимых рыночных цен")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="рассчитать НМЦД по файлу ТЗ (DOCX, PDF, TXT)")
    a.add_argument("file", type=Path)
    a.add_argument("--out", type=Path, default=None, help="папка для отчётов")
    a.add_argument("--no-llm", action="store_true", help="не использовать языковую модель")
    a.add_argument("--no-web", action="store_true", help="не искать на сайтах поставщиков")
    a.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "analyze":
        if not args.file.is_file():
            print(f"Файл не найден: {args.file}", file=sys.stderr)
            return 2
        return asyncio.run(analyze(args.file, args.out, not args.no_llm, not args.no_web, args.quiet))
    return 2


if __name__ == "__main__":
    sys.exit(main())
