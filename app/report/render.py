"""Выгрузка отчёта: HTML, JSON, XLSX с формулами и DOCX по форме обоснования НМЦД."""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import CalcStatus, Report, SourceKind
from app.report.fmt import dt, money, num, pct, rubles_in_words
from app.report.view import CODE_RU, SOURCE_RU, report_view

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(["html"]))
_env.filters["nb"] = lambda v: Markup(str(escape(v)).replace(" ", "&nbsp;"))   # числа без переноса


def render_html(report: Report, interactive: bool = False, links: bool = False,
                actions: Optional[list[dict]] = None, banner: Optional[dict] = None, app_nav: bool = False,
                site_base: str = "") -> str:
    """HTML-отчёт. interactive — решения специалиста и ввод КП; links — ссылки на копии страниц в сервисе;
    actions — кнопки выгрузки; banner — пояснение над отчётом; app_nav — навигация сервиса в шапке;
    site_base — относительный путь к корню статического сайта с готовыми отчётами."""
    return _env.get_template("report.html").render(**report_view(report), interactive=interactive,
                                                   links=interactive or links, actions=actions or [],
                                                   banner=banner, app_nav=app_nav, site_base=site_base)


def report_actions(base: str, report: Report, docx: str = "obosnovanie_nmcd.docx", xlsx: str = "report.xlsx",
                   kp: str = "zapros_kp.docx") -> list[dict]:
    """Кнопки выгрузки: документ обоснования, расчёт XLSX и запрос КП, если цена сформирована не везде."""
    acts = [{"label": "Скачать обоснование (DOCX)", "href": base + docx, "primary": True},
            {"label": "Таблица расчёта (XLSX)", "href": base + xlsx}]
    if report.items and not report.all_final:
        acts.append({"label": "Запрос коммерческих предложений (DOCX)", "href": base + kp})
    return acts


def _price_sources(report: Report) -> str:
    """Источники цен этого расчёта: реестр ЕИС всегда, сайты — если поиск по ним был включён, КП — если внесены."""
    out = ["реестр контрактов ЕИС (исполненные контракты за последние три года)"]
    if report.settings.get("web"):
        out.append("сайты поставщиков (публичные предложения)")
    if any(o.source == SourceKind.MANUAL for r in report.items for o in r.offers):
        out.append("коммерческие предложения поставщиков (п. 3.7.1)")
    return "; ".join(out)


def render_json(report: Report) -> str:
    return report.model_dump_json(indent=2)


# ------------------------------------------------------------------ XLSX

def render_xlsx(report: Report) -> bytes:
    wb = Workbook()
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EEF2F6")
    wrap = Alignment(wrap_text=True, vertical="top")

    ws = wb.active
    ws.title = "Сводка"
    ws["A1"] = "Обоснование начальной (максимальной) цены договора"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = report.procurement.title
    ws["A3"] = f"Дата расчёта: {report.settings.get('calc_date', '')}; документ: {report.procurement.source_name}"
    ws.append([])
    ws.append(["№", "Позиция", "Количество", "Единица", "Статус", "Цена за единицу, руб.", "НМЦД позиции, руб."])
    for c in ws[5]:
        c.font = bold
        c.fill = head_fill

    calc_ws = wb.create_sheet("Расчёт")
    calc_ws.append(["Расчёт по формуле п. 3.21 приказа № 567: НМЦД = количество × среднее арифметическое приведённых цен"])
    calc_ws["A1"].font = bold
    row = 3
    summary_rows = []
    for res in report.items:
        it, calc = res.item, res.calc
        calc_ws.cell(row=row, column=1, value=f"Позиция {it.number}. {it.name}").font = bold
        row += 1
        headers = ["№", "Источник", "Поставщик", "ИНН", "Ссылка", "Дата цены", "Исходная цена, руб.", "Единица источника",
                   "Цена за единицу закупки, руб."]
        for j, h in enumerate(headers, start=1):
            cell = calc_ws.cell(row=row, column=j, value=h)
            cell.font = bold
            cell.fill = head_fill
        row += 1
        first = row
        norms = {n.offer_id: n for n in res.normalizations}
        included = [d.offer_id for d in res.decisions if d.included]
        offers = {o.id: o for o in res.offers}
        for k, oid in enumerate(included, start=1):
            o, n = offers[oid], norms.get(oid)
            calc_ws.cell(row=row, column=1, value=k)
            calc_ws.cell(row=row, column=2, value=SOURCE_RU.get(o.source, str(o.source)))
            calc_ws.cell(row=row, column=3, value=o.supplier_name or "")
            calc_ws.cell(row=row, column=4, value=o.supplier_inn or "")
            link = calc_ws.cell(row=row, column=5, value=o.url)
            link.hyperlink = o.url
            calc_ws.cell(row=row, column=6, value=dt(o.price_date))
            calc_ws.cell(row=row, column=7, value=float(o.price) if o.price else None)
            calc_ws.cell(row=row, column=8, value=o.price_unit)
            calc_ws.cell(row=row, column=9, value=float(n.unit_price) if n and n.unit_price is not None else None)
            calc_ws.cell(row=row, column=9).number_format = "#,##0.00"
            row += 1
        last = row - 1
        rng = f"I{first}:I{last}" if included else None
        lines = [
            ("Количество цен", f"=COUNT({rng})" if rng else 0, "0"),
            ("Среднее арифметическое, руб.", f"=AVERAGE({rng})" if rng else None, "#,##0.00"),
            ("Стандартное отклонение (n−1), руб.", f"=IF(COUNT({rng})>1,STDEV({rng}),\"\")" if rng else None, "#,##0.00"),
            ("Коэффициент вариации", f"=IF(COUNT({rng})>1,STDEV({rng})/AVERAGE({rng}),\"\")" if rng else None, "0.00%"),
            ("Количество закупаемого товара", float(it.quantity), "#,##0.###"),
        ]
        stat_rows = {}
        for label, formula, fmt in lines:
            calc_ws.cell(row=row, column=8, value=label).font = bold
            c = calc_ws.cell(row=row, column=9, value=formula)
            c.number_format = fmt
            stat_rows[label] = row
            row += 1
        final = calc is not None and calc.status == CalcStatus.FINAL
        calc_ws.cell(row=row, column=8, value="НМЦД позиции, руб.").font = bold
        if final and rng:
            c = calc_ws.cell(row=row, column=9,
                             value=f"=ROUND(I{stat_rows['Количество закупаемого товара']}*ROUND(I{stat_rows['Среднее арифметическое, руб.']},2),2)")
        else:
            c = calc_ws.cell(row=row, column=9, value=(calc.message if calc else "Цена не сформирована: нет данных."))
        c.number_format = "#,##0.00"
        c.font = bold
        summary_rows.append((it, calc, row if final and rng else None, stat_rows.get("Среднее арифметическое, руб.")))
        row += 2

    for it, calc, total_row, mean_row in summary_rows:
        final = calc is not None and calc.status == CalcStatus.FINAL
        ws.append([it.number, it.name, float(it.quantity), it.unit,
                   "обоснована" if final else "недостаточно данных",
                   f"=ROUND('Расчёт'!I{mean_row},2)" if final and mean_row else None,
                   f"='Расчёт'!I{total_row}" if total_row else None])
        r = ws.max_row
        ws.cell(row=r, column=6).number_format = "#,##0.00"
        ws.cell(row=r, column=7).number_format = "#,##0.00"
    ws.append([])
    end = ws.max_row - 1
    ws.append(["", "Итого НМЦД по обоснованным позициям, руб.", "", "", "", "", f"=SUM(G6:G{end})"])
    ws.cell(row=ws.max_row, column=7).number_format = "#,##0.00"
    ws.cell(row=ws.max_row, column=2).font = bold
    ws.cell(row=ws.max_row, column=7).font = bold

    off = wb.create_sheet("Предложения")
    off.append(["Позиция", "Код", "Источник", "Ссылка", "Наименование", "Поставщик", "ИНН", "Дата цены", "Получено",
                "Цена, руб.", "Единица", "Содержимое", "Цена за единицу закупки, руб.", "Класс", "Решение", "Причина",
                "Приведение", "SHA-256 копии"])
    for c in off[1]:
        c.font = bold
        c.fill = head_fill
    for res in report.items:
        matches = {m.offer_id: m for m in res.matches}
        norms = {n.offer_id: n for n in res.normalizations}
        decs = {d.offer_id: d for d in res.decisions}
        for o in res.offers:
            m, n, d = matches.get(o.id), norms.get(o.id), decs.get(o.id)
            off.append([res.item.number, o.id, SOURCE_RU.get(o.source, str(o.source)), o.url, o.title,
                        o.supplier_name or "", o.supplier_inn or "", dt(o.price_date), dt(o.retrieved_at, True),
                        float(o.price) if o.price else None, o.price_unit,
                        f"{o.content.value} {o.content.unit}" if o.content else "",
                        float(n.unit_price) if n and n.unit_price is not None else None,
                        m.cls.value if m else "", CODE_RU.get(d.code, d.code.value) if d else "",
                        d.reason if d else "", "; ".join(s.note for s in (n.steps if n else [])),
                        o.evidence.sha256 if o.evidence else ""])
            off.cell(row=off.max_row, column=4).hyperlink = o.url

    log = wb.create_sheet("Журнал")
    log.append(["Время (UTC)", "Этап", "Событие"])
    for c in log[1]:
        c.font = bold
    for e in report.trace:
        log.append([e.ts.strftime("%Y-%m-%d %H:%M:%S"), e.stage, e.message])

    widths = {"Сводка": [5, 60, 12, 10, 20, 20, 22], "Расчёт": [5, 16, 34, 14, 40, 12, 18, 16, 26],
              "Предложения": [9, 12, 16, 40, 50, 30, 14, 12, 18, 14, 12, 14, 16, 12, 22, 60, 60, 20],
              "Журнал": [20, 12, 120]}
    for name, ws_w in widths.items():
        sheet = wb[name]
        for j, w in enumerate(ws_w, start=1):
            sheet.column_dimensions[get_column_letter(j)].width = w
        for r in sheet.iter_rows(min_row=1, max_row=sheet.max_row):
            for c in r:
                if isinstance(c.value, str) and len(c.value) > 40:
                    c.alignment = wrap
    props = wb.properties
    props.creator = report.settings.get("org_name") or "Заказчик"
    props.lastModifiedBy = props.creator
    props.title = "Обоснование НМЦД"
    buf = io.BytesIO()
    wb.save(buf)
    return _finish_xlsx(buf.getvalue(), props.creator or "")


def _finish_xlsx(data: bytes, company: str) -> bytes:
    """Свойства книги без подписи библиотеки-генератора: приложение «Microsoft Excel», организация."""
    import zipfile
    from xml.sax.saxutils import escape

    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            content = src.read(info.filename)
            if info.filename == "docProps/app.xml":
                xml = content.decode("utf-8")
                xml = re.sub(r"<Application>.*?</Application>", "<Application>Microsoft Excel</Application>", xml,
                             flags=re.S)
                xml = re.sub(r"<AppVersion>.*?</AppVersion>", "<AppVersion>16.0300</AppVersion>", xml, flags=re.S)
                if "<Company" in xml:
                    xml = re.sub(r"<Company\s*/>|<Company>.*?</Company>", f"<Company>{escape(company)}</Company>", xml,
                                 flags=re.S)
                else:
                    xml = xml.replace("</Properties>", f"<Company>{escape(company)}</Company></Properties>")
                content = xml.encode("utf-8")
            z.writestr(info, content)
    return out.getvalue()


# ------------------------------------------------------------------ DOCX

def _set_cell(cell, text: str, bold: bool = False, size: int = 10, align: Optional[int] = None) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if align is not None:
        p.alignment = align


def _table(document, headers: list[str], rows: list[list[str]], widths_cm: list[float]):
    """Таблица с фиксированной раскладкой: ширины колонок задаются в сетке, у ячеек и у таблицы,
    чтобы Word и LibreOffice показывали её одинаково и она не выходила за поля."""
    t = document.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    tbl_pr = t._tbl.tblPr
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:type"), "dxa")
    tbl_w.set(qn("w:w"), str(int(sum(widths_cm) * 567)))
    for j, w in enumerate(widths_cm):
        t.columns[j].width = Cm(w)
    for j, h in enumerate(headers):
        _set_cell(t.rows[0].cells[j], h, bold=True, size=9)
    for row in rows:
        cells = t.add_row().cells
        for j, v in enumerate(row):
            _set_cell(cells[j], v, size=9,
                      align=WD_ALIGN_PARAGRAPH.RIGHT if re.match(r"^[\d\s,]+(?: ₽)?$", v or "") else None)
    for row in t.rows:
        for j, w in enumerate(widths_cm):
            row.cells[j].width = Cm(w)
    return t


def _heading(d, text: str):
    """Заголовок раздела: полужирный, не отрывается от следующего абзаца или таблицы."""
    p = d.add_paragraph()
    p.paragraph_format.keep_with_next = True
    p.paragraph_format.space_before = Pt(6)
    p.add_run(text).bold = True
    return p


def render_docx(report: Report, org_name: str = "Заказчик") -> bytes:
    d = docx.Document()
    sec = d.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Cm(2.5))
    st = d.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    st.paragraph_format.line_spacing = 1.15
    st.paragraph_format.space_after = Pt(4)

    h = d.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = h.add_run("Обоснование начальной (максимальной) цены договора")
    r.bold = True
    r.font.size = Pt(14)

    proc = report.procurement
    calc_date = report.settings.get("calc_date", "")
    for label, value in [
        ("Предмет закупки", proc.title),
        ("Метод определения", "метод сопоставимых рыночных цен (анализ рынка)"),
        ("Основание", "положение о закупке заказчика; Методические рекомендации по применению методов определения "
                      "начальной (максимальной) цены, утверждённые приказом Минэкономразвития России от 02.10.2013 № 567"),
        ("Источники ценовой информации", _price_sources(report)),
        ("Дата расчёта", calc_date and datetime.fromisoformat(calc_date).strftime("%d.%m.%Y")),
    ]:
        p = d.add_paragraph()
        p.add_run(f"{label}: ").bold = True
        p.add_run(str(value or "—"))
    p = d.add_paragraph()
    p.add_run("Порядок расчёта. ").bold = True
    rules = report.settings.get("rules", {})
    m = rules.get("method", {})
    p.add_run(
        f"Цены приведены к единице закупки (единица измерения, содержимое упаковки, НДС, доставка; цены старше "
        f"{m.get('cpi_threshold_months', 6)} месяцев — по индексу потребительских цен). В расчёт включены цены "
        f"идентичных товаров, а при их недостатке — однородных. Аномальные значения исключены по правилу "
        f"медианы и MAD. Однородность проверена коэффициентом вариации (не более "
        f"{int(float(m.get('cv_threshold', 0.33)) * 100)} %). НМЦД позиции = количество × среднее арифметическое цен.")
    if report.settings.get("legal_checks"):
        p.add_run(" Не использованы цены контрактов, по которым взыскивались неустойки, и цены поставщиков, "
                  "включённых в реестр недобросовестных поставщиков (п. 3.13).")

    _heading(d, "1. Расчёт по позициям")
    rows = []
    for res in report.items:
        c = res.calc
        final = c is not None and c.status == CalcStatus.FINAL
        rows.append([str(res.item.number), res.item.name, res.item.unit, num(res.item.quantity, 0) if res.item.quantity == res.item.quantity.to_integral() else num(res.item.quantity, 3),
                     money(c.unit_price, "") if final else "—", money(c.total, "") if final else "—",
                     pct(c.cv) if c and c.cv is not None else "—",
                     "обоснована" if final else "недостаточно данных"])
    _table(d, ["№", "Наименование", "Ед.", "Кол-во", "Цена за ед., руб.", "НМЦД, руб.", "CV", "Статус"], rows,
           [0.7, 4.4, 1.2, 1.3, 2.0, 2.3, 1.6, 2.5])
    p = d.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    if report.total:
        p.add_run(f"Начальная (максимальная) цена договора по обоснованным позициям: {money(report.total, '')} руб. "
                  f"({rubles_in_words(report.total)}).").bold = True
    else:
        p.add_run("Обоснованная цена не сформирована: недостаточно надёжных ценовых данных.").bold = True
    if not report.all_final and report.items:
        d.add_paragraph("По позициям со статусом «недостаточно данных» цена не формируется: требуется запрос "
                        "коммерческих предложений не менее чем у пяти поставщиков (п. 3.7.1).")

    _heading(d, "2. Ценовая информация по позициям")
    refs: list[tuple[str, str]] = []           # [номер] → полный адрес источника (раздел 3)
    for res in report.items:
        _heading(d, f"Позиция {res.item.number}. {res.item.name}")
        offers = {o.id: o for o in res.offers}
        norms = {n.offer_id: n for n in res.normalizations}
        inc = [x.offer_id for x in res.decisions if x.included]
        rows = []
        for k, oid in enumerate(inc, start=1):
            o, n = offers[oid], norms.get(oid)
            if o.source == SourceKind.MANUAL:
                # п. 2.1: в обосновании наименования поставщиков, представивших КП, не указываются
                cell = f"Коммерческое предложение № {o.extra.get('kp_number', k)}"
            else:
                refs.append((o.url, f"получено {dt(o.retrieved_at)}"
                                    + (f", копия страницы SHA-256 {o.evidence.sha256[:16]}…"
                                       if o.evidence and o.evidence.sha256 else "")))
                src = (f"ЕИС, контракт № {o.contract_number}" if o.contract_number else f"сайт {o.source_name}")
                cell = f"{src} [{len(refs)}]"
            rows.append([str(k), cell, dt(o.price_date), money(o.price, ""),
                         money(n.unit_price, "") if n and n.unit_price is not None else "—"])
        if rows:
            _table(d, ["№", "Источник ценовой информации", "Дата цены", "Цена исходная, руб.",
                       "Цена приведённая, руб."], rows, [0.7, 7.6, 2.3, 2.6, 2.8])
        c = res.calc
        if c is not None:
            if c.status == CalcStatus.FINAL:
                d.add_paragraph(f"Цен в расчёте: {c.n} (поставщиков: {c.n_suppliers}); среднее {money(c.mean)}; "
                                f"коэффициент вариации {pct(c.cv)}. НМЦД = {num(res.item.quantity, 0)} × "
                                f"{money(c.unit_price)} = {money(c.total)}.")
            else:
                d.add_paragraph(c.message)
        excluded = sum(1 for x in res.decisions if not x.included)
        if excluded:
            d.add_paragraph(f"Исключено предложений: {excluded}. Причины по каждому предложению приведены в "
                            f"электронном отчёте (идентификатор расчёта {report.id}).").runs[0].font.size = Pt(10)

    if refs:
        _heading(d, "3. Адреса источников")
        for k, (url, note) in enumerate(refs, start=1):
            p = d.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(f"[{k}] {url} ({note})")
            run.font.size = Pt(9)

    d.add_paragraph()
    for label in ("Ответственный сотрудник контрактной службы", "Должность", "Подпись / ФИО", "Дата"):
        d.add_paragraph(f"{label}: ______________________________")

    cp = d.core_properties
    cp.author = org_name
    cp.last_modified_by = org_name
    cp.created = cp.modified = (report.finished_at or report.created_at).replace(tzinfo=None)
    cp.revision = 1
    cp.title = "Обоснование НМЦД"
    cp.subject = proc.title[:200]
    cp.comments = ""
    cp.keywords = ""
    cp.category = ""
    buf = io.BytesIO()
    d.save(buf)
    return _set_company(buf.getvalue(), org_name)


def _set_company(data: bytes, company: str) -> bytes:
    """Заменяет значение <Company> в docProps/app.xml (элемент уже есть в шаблоне — не дублируем)
    и меняет неразрывные пробелы в тексте на обычные: Word со знаками форматирования показывает их как «°»."""
    import zipfile
    from xml.sax.saxutils import escape

    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            content = src.read(info.filename)
            if info.filename == "docProps/app.xml":
                xml = content.decode("utf-8")
                if "<Company" in xml:
                    xml = re.sub(r"<Company\s*/>|<Company>.*?</Company>", f"<Company>{escape(company)}</Company>", xml,
                                 flags=re.S)
                else:
                    xml = xml.replace("</Properties>", f"<Company>{escape(company)}</Company></Properties>")
                content = xml.encode("utf-8")
            elif info.filename.startswith("word/") and info.filename.endswith(".xml"):
                content = content.decode("utf-8").replace("\u00a0", " ").replace("\u202f", " ").encode("utf-8")
            z.writestr(info, content)
    return out.getvalue()


def save_all(report: Report, out_dir: Path, org_name: str = "Заказчик") -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report.settings.setdefault("org_name", org_name)
    paths = {
        "json": out_dir / "report.json",
        "html": out_dir / "report.html",
        "xlsx": out_dir / "report.xlsx",
        "docx": out_dir / "obosnovanie_nmcd.docx",
    }
    paths["json"].write_text(render_json(report), encoding="utf-8")
    paths["html"].write_text(render_html(report, actions=report_actions("", report)), encoding="utf-8")
    paths["xlsx"].write_bytes(render_xlsx(report))
    paths["docx"].write_bytes(render_docx(report, org_name))
    if report.items and not report.all_final:
        from app.report.kp import render_kp_docx
        paths["kp"] = out_dir / "zapros_kp.docx"
        paths["kp"].write_bytes(render_kp_docx(report, org_name))
    return paths
