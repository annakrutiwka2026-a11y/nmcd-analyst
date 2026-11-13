"""Кладёт рядом с примерами копии страниц-источников, на которые ссылаются их отчёты (examples/evidence).

Так готовые отчёты по примерам проверяемы в любом окружении: в веб-интерфейсе ссылка «копия» открывает
сохранённую страницу, а SHA-256 файла совпадает с записанным в отчёте.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.models import Report  # noqa: E402


def main() -> int:
    src = get_settings().data_dir / "evidence"
    dst = ROOT / "examples" / "evidence"
    dst.mkdir(parents=True, exist_ok=True)
    need: set[str] = set()
    for path in sorted(ROOT.glob("examples/reports*/*/report.json")):
        rep = Report.model_validate_json(path.read_text(encoding="utf-8"))
        for r in rep.items:
            for o in r.offers:
                if o.evidence and o.evidence.snapshot_path:
                    need.add(Path(o.evidence.snapshot_path).name)
    copied, missing = 0, []
    for name in sorted(need):
        f = src / name
        if not f.is_file():
            missing.append(name)
            continue
        shutil.copyfile(f, dst / name)
        copied += 1
    for old in dst.glob("*.html.gz"):
        if old.name not in need:
            old.unlink()
    size = sum(p.stat().st_size for p in dst.glob("*.html.gz"))
    print(f"копий: {copied}, нет в хранилище: {len(missing)}, объём {size / 1e6:.1f} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
