"""Статический сайт с готовыми расчётами (GitHub Pages): главная, отчёты примеров, методика, видео, презентация.

python tools/build_site.py <папка сайта> <адрес репозитория>
Новый расчёт на статическом сайте не запускается: для него сервис поднимается одной командой (README).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from app.engine.rules import load_rules  # noqa: E402
from app.models import CalcStatus, Report  # noqa: E402
from app.report.fmt import money  # noqa: E402
from app.report.render import render_html, report_actions  # noqa: E402
from app.report.view import _final_label  # noqa: E402
from app.web.examples import EXAMPLE_REPORTS, EXAMPLES  # noqa: E402

FILES = ("obosnovanie_nmcd.docx", "report.xlsx", "zapros_kp.docx")


def _video_len(path: Path) -> str:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True).stdout
        sec = round(float(out))
        return f"{sec // 60} мин {sec % 60} с" if sec >= 60 else f"{sec} с"
    except (OSError, ValueError, subprocess.CalledProcessError):
        return "около минуты"


def main() -> int:
    out, repo_url = Path(sys.argv[1]), sys.argv[2]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    env = Environment(loader=FileSystemLoader([str(ROOT / "tools" / "site"), str(ROOT / "app" / "web" / "templates"),
                                               str(ROOT / "app" / "report" / "templates")]),
                      autoescape=select_autoescape(["html"]))
    rows, calc_date = [], ""
    for key, (_, title, meta) in EXAMPLES.items():
        row = {"key": key, "title": title, "meta": meta, "no_keys": False, "total": "—", "final": ""}
        for mode, folder in EXAMPLE_REPORTS.items():
            src = ROOT / "examples" / folder / key
            if not (src / "report.json").is_file():
                continue
            rep = Report.model_validate_json((src / "report.json").read_text(encoding="utf-8"))
            dst = out / "examples" / mode / key
            dst.mkdir(parents=True)
            for name in FILES:
                if (src / name).is_file():
                    shutil.copy(src / name, dst / name)
            how = ("Цены взяты из реестра контрактов ЕИС и с сайтов поставщиков, техническое задание разобрано моделью."
                   if mode == "full" else "Расчёт без ключей: цены только из реестра контрактов ЕИС, ТЗ разобрано по правилам.")
            banner = {"title": f"Готовый расчёт по примеру «{title}»", "text": how}
            actions = [a for a in report_actions("", rep) if (dst / a["href"]).is_file()]
            (dst / "index.html").write_text(render_html(rep, actions=actions, banner=banner, site_base="../../../"),
                                            encoding="utf-8")
            if mode == "full":
                n_final = sum(1 for r in rep.items if r.calc and r.calc.status == CalcStatus.FINAL)
                row["total"] = money(rep.total) if rep.total else "не сформирована"
                label = _final_label(n_final, len(rep.items))
                row["final"] = "цена обоснована" if label == "да" else (f"цена обоснована {label}"
                                                                         if label.startswith("по ") else label)
                calc_date = ".".join(reversed((rep.settings.get("calc_date") or "").split("-")))
            else:
                row["no_keys"] = True
        rows.append(row)
    pres = ROOT / "presentation"
    for name in ("nmcd_presentation.pdf", "nmcd_demo.mp4"):
        if (pres / name).is_file():
            shutil.copy(pres / name, out / name)
    (out / "index.html").write_text(env.get_template("index.html").render(
        examples=rows, calc_date=calc_date, repo_url=repo_url, site_base="./",
        video_len=_video_len(out / "nmcd_demo.mp4")), encoding="utf-8")
    (out / "method.html").write_text(env.get_template("method.html").render(rules=load_rules(), site_base="./"),
                                     encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"сайт: {out} ({sum(1 for _ in out.rglob('*') if _.is_file())} файлов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
