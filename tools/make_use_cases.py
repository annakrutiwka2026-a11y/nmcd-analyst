"""Собирает docs/USE_CASES.md из отчётов примеров.

Полный режим — examples/reports/<пример>/report.json (модель, сайты поставщиков, ЕИС);
режим без ключей — examples/reports_no_keys/<пример>/report.json (правила и ЕИС, так сервис работает сразу после
`docker compose up`). Числа в документе берутся только из отчётов, руками не набираются.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import CalcStatus, Report, SourceKind  # noqa: E402
from app.report.fmt import money, pct  # noqa: E402
from app.report.view import CODE_RU  # noqa: E402

CASES = [
    ("paper", "tz_paper.docx", "Бумага для офисной техники",
     "ТЗ по форме МФТИ (DOCX): 3 позиции, товарный знак в ТЗ, характеристики в таблице, фасовка 500 листов"),
    ("printer", "tz_3d_printer.docx", "3D-принтер конкретной модели",
     "ТЗ по форме МФТИ (DOCX): 1 позиция, модель названа точно, 24 характеристики одним списком"),
    ("cytokines", "tz_cytokines.docx", "Рекомбинантные белки (цитокины)",
     "ТЗ по форме МФТИ (DOCX): 5 позиций, фасовка «не менее 10 / 100 мкг», клеточная линия, чистота, эндотоксин"),
    ("cartridge", "opisanie_kartridzh.txt", "Картридж по текстовому описанию",
     "простой текст из одной строки, категория вне материалов кейса"),
]

MODES = [("reports", "Полный режим: модель, сайты поставщиков, ЕИС", ""),
         ("reports_no_keys", "Без ключей: разбор по правилам, только ЕИС", " --no-llm --no-web")]


def _load(folder: str, key: str) -> Report | None:
    path = ROOT / "examples" / folder / key / "report.json"
    if not path.is_file():
        return None
    return Report.model_validate_json(path.read_text(encoding="utf-8"))


def _table(rep: Report) -> list[str]:
    out = ["| № | Позиция | Кол-во | Найдено: ЕИС / сайты | В расчёте | CV | Цена за ед. | НМЦД позиции | Итог по позиции |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rep.items:
        c = r.calc
        eis = sum(1 for o in r.offers if o.source == SourceKind.EIS44)
        web = sum(1 for o in r.offers if o.source == SourceKind.WEB)
        final = c is not None and c.status == CalcStatus.FINAL
        out.append(f"| {r.item.number} | {r.item.name[:60]} | {r.item.quantity:f} {r.item.unit} | {eis} / {web} | "
                   f"{c.n if c else 0} | {pct(c.cv) if c and c.cv is not None else '—'} | "
                   f"{money(c.unit_price) if final else '—'} | {money(c.total) if final else '—'} | "
                   f"{'цена обоснована' if final else 'цена не сформирована'} |")
    return out


def _excluded(rep: Report) -> str:
    cnt: Counter[str] = Counter()
    for r in rep.items:
        for d in r.decisions:
            if not d.included:
                cnt[CODE_RU.get(d.code, str(d.code))] += 1
    return "; ".join(f"{k} — {v}" for k, v in cnt.most_common()) or "нет"


def main() -> int:
    lines = ["# Примеры использования", "",
             "Четыре сценария: три ТЗ из материалов кейса и одно описание текстом из другой категории. Каждый пример",
             "посчитан в двух режимах. Отчёты лежат в `examples/reports/<пример>/` (полный режим) и",
             "`examples/reports_no_keys/<пример>/` (без ключей): `report.html`, `obosnovanie_nmcd.docx`, `report.xlsx`",
             "с формулами, `report.json` с журналом шагов и `zapros_kp.docx`, если цена по позиции не сформирована.", "",
             "Цены в ЕИС и на сайтах меняются, поэтому при повторном запуске числа могут отличаться; шаги расчёта",
             "и правила отбора остаются теми же. Числа ниже взяты из приложенных отчётов.", ""]
    for key, tz, title, about in CASES:
        reps = [(m, _load(m[0], key)) for m in MODES]
        if not any(r for _, r in reps):
            continue
        lines += [f"## {title}", "", f"**Вход:** `examples/tz/{tz}` — {about}.", ""]
        for (folder, mode_title, flags), rep in reps:
            if rep is None:
                continue
            lines += [f"### {mode_title}", "", "```bash",
                      f"docker compose run --rm nmcd python -m app.cli analyze examples/tz/{tz}{flags} --out /data/out/{key}",
                      "```", "", f"Дата расчёта {rep.settings.get('calc_date')}.", ""]
            lines += _table(rep)
            lines += ["", f"**НМЦД по обоснованным позициям:** {money(rep.total)}.",
                      f"**Исключено предложений:** {_excluded(rep)}.", ""]
            for r in rep.items:
                if r.calc and r.calc.status != CalcStatus.FINAL:
                    lines.append(f"- Позиция {r.item.number}: {r.calc.message}")
            lines.append("")
    (ROOT / "docs" / "USE_CASES.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("docs/USE_CASES.md обновлён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
