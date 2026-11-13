"""Черновик запроса ценовой информации (коммерческих предложений) по позициям, где данных не хватило.

Состав запроса — по п. 3.7–3.12 Методических рекомендаций (приказ № 567): описание объекта и характеристик,
количество, условия поставки и оплаты, просьба указать цену с НДС и состав цены, срок действия предложения,
оговорка, что запрос не порождает обязательств. Список адресатов — поставщики с исполненными контрактами по таким
же товарам (из ЕИС) и продавцы, найденные на сайтах. Письма отправляет специалист, сервис ничего не отправляет сам.
"""
from __future__ import annotations

import io
from datetime import date

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.engine.matching import condition_text
from app.models import CalcStatus, Report, SourceKind
from app.report.render import _set_company, _table
from app.report.view import quantity_label, supplier_label


def suppliers_for(report: Report, item_id: str, limit: int = 10) -> list[tuple[str, str, str]]:
    """Возможные адресаты: (наименование, ИНН или домен, откуда известен)."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for res in report.items:
        if res.item.id != item_id:
            continue
        for o in res.offers:
            if o.extra.get("relevant") is False:
                continue
            key = o.supplier_inn or o.source_name
            if not key or key in seen:
                continue
            seen.add(key)
            where = (f"ЕИС, контракт № {o.contract_number}" if o.source == SourceKind.EIS44 else f"сайт {o.source_name}")
            out.append((supplier_label(o.supplier_name or o.source_name), o.supplier_inn or o.source_name, where))
    return out[:limit]


def render_kp_docx(report: Report, org_name: str = "Заказчик") -> bytes:
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

    pending = [r for r in report.items if not (r.calc and r.calc.status == CalcStatus.FINAL)]
    targets = pending or report.items
    h = d.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = h.add_run("Запрос ценовой информации")
    run.bold = True
    run.font.size = Pt(14)
    d.add_paragraph(f"{org_name} просит направить коммерческое предложение на поставку товара по приведённому "
                    f"ниже описанию. Предложение используется для определения начальной (максимальной) цены договора "
                    f"и не порождает обязательств сторон.")
    d.add_paragraph(f"Предмет закупки: {report.procurement.title}.")
    if report.procurement.delivery_terms:
        d.add_paragraph(f"Срок поставки: {report.procurement.delivery_terms.rstrip('. ')}.")
    d.add_paragraph("Место поставки: по адресу заказчика, доставка, разгрузка и все расходы включаются в цену.")
    for res in targets:
        it = res.item
        d.add_paragraph().add_run(f"Позиция {it.number}. {it.name} — {quantity_label(it.quantity, it.unit)}").bold = True
        if it.content is not None:
            d.add_paragraph(f"Содержимое одной единицы: {'не менее ' if it.content_op.value == '>=' else ''}"
                            f"{quantity_label(it.content.value, it.content.unit)}.")
        rows = []
        for q in it.requirements:
            rows.append([q.name, condition_text(q)])
        if rows:
            _table(d, ["Характеристика", "Требование"], rows, [8.0, 8.0])
    d.add_paragraph()
    d.add_paragraph("Просим указать: цену за единицу и общую стоимость с НДС (или основание его неприменения), что "
                    "включено в цену (доставка, разгрузка, гарантия), срок поставки, условия оплаты, срок действия "
                    "предложения (не менее 30 дней), производителя и страну происхождения, наименование и ИНН "
                    "организации. Предложение просим направить в течение 5 рабочих дней.")
    d.add_paragraph(f"Дата запроса: {date.today().strftime('%d.%m.%Y')}.")
    d.add_paragraph("Ответственный сотрудник: ______________________________")

    d.add_page_break()
    d.add_paragraph().add_run("Возможные адресаты запроса (не менее пяти, п. 3.7.1)").bold = True
    for res in targets:
        sup = suppliers_for(report, res.item.id)
        d.add_paragraph(f"Позиция {res.item.number}. {res.item.short_name or res.item.name}")
        if sup:
            _table(d, ["Поставщик", "ИНН / сайт", "Откуда известен"], [list(s) for s in sup], [6.5, 3.5, 6.0])
        else:
            d.add_paragraph("Поставщики не найдены: подберите адресатов в каталоге ЕИС или среди производителей.")

    cp = d.core_properties
    cp.author = org_name
    cp.last_modified_by = org_name
    cp.created = cp.modified = (report.finished_at or report.created_at).replace(tzinfo=None)
    cp.revision = 1
    cp.title = "Запрос ценовой информации"
    cp.comments = ""
    buf = io.BytesIO()
    d.save(buf)
    return _set_company(buf.getvalue(), org_name)
