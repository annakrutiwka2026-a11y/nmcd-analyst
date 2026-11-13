"""Отчёты: HTML, XLSX с формулами, DOCX без следов генератора, запрос КП."""
import io
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

from openpyxl import load_workbook

from app.models import (CalcResult, CalcStatus, Decision, DecisionCode, Evidence, Item, ItemResult, MatchClass,
                        MatchResult, Normalization, NormStep, Offer, Procurement, Quantity, Report, SourceKind)
from app.report.kp import render_kp_docx
from app.report.render import render_docx, render_html, render_xlsx

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def make_report() -> Report:
    item = Item(id="item1", number=1, name="Бумага для офисной техники А4", quantity=Decimal("2500"), unit="пачка",
                content=Quantity(value=Decimal("500"), unit="лист"))
    prices = [Decimal("313.46"), Decimal("319.00"), Decimal("334.00"), Decimal("1200.00")]
    offers, matches, norms, decs = [], [], [], []
    for k, p in enumerate(prices, start=1):
        oid = f"item1-e{k}"
        offers.append(Offer(id=oid, item_id="item1", source=SourceKind.EIS44, source_name="ЕИС, реестр контрактов 44-ФЗ",
                            url=f"https://zakupki.gov.ru/x?{k}", retrieved_at=NOW, title="Бумага SvetoCopy А4",
                            supplier_name=f"ООО «Поставщик {k}»", supplier_inn=f"77000000{k:02d}", price=p,
                            price_unit="пачка", price_date=NOW, contract_number=f"10{k}",
                            evidence=Evidence(url="u", retrieved_at=NOW, snapshot_path="evidence/a.html.gz", sha256="ab" * 32)))
        matches.append(MatchResult(offer_id=oid, cls=MatchClass.IDENTICAL, score=0.9, reasons=["совпали формат и масса"]))
        norms.append(Normalization(offer_id=oid, ok=True, unit_price=p,
                                   steps=[NormStep(kind="unit", factor=Decimal(1), note="цена за пачку")]))
        if k < 4:
            decs.append(Decision(offer_id=oid, included=True, code=DecisionCode.INCLUDED, reason="идентичное"))
        else:
            decs.append(Decision(offer_id=oid, included=False, code=DecisionCode.OUTLIER_RATIO,
                                 reason="цена выше медианы в 3,7 раза"))
    mean = sum(prices[:3]) / 3
    calc = CalcResult(status=CalcStatus.FINAL, n=3, n_suppliers=3, prices=prices[:3], offer_ids=["item1-e1", "item1-e2", "item1-e3"],
                      mean=mean, median=Decimal("319.00"), min=prices[0], max=prices[2], sd=Decimal("10.8"),
                      cv=Decimal("0.0338"), unit_price=mean.quantize(Decimal("0.01")),
                      total=(mean * 2500).quantize(Decimal("0.01")), message="цена обоснована")
    res = ItemResult(item=item, offers=offers, matches=matches, normalizations=norms, decisions=decs, calc=calc)
    proc = Procurement(title="Закупка бумаги", items=[item], source_name="tz.docx", source_sha256="0" * 64)
    return Report(id="abcdef123456", created_at=NOW, finished_at=NOW, procurement=proc, items=[res],
                  total=calc.total, status="done", all_final=True,
                  settings={"llm": False, "web": False, "calc_date": "2026-11-12", "cpi": "загружен",
                            "rules": {"method": {"min_suppliers": 3, "cv_threshold": 0.33, "max_age_months": 36,
                                                 "cpi_threshold_months": 6},
                                      "outliers": {"mad_z": 3.5, "ratio_flag": 1.5, "ratio_exclude": 2.0}}})


def test_html_contains_sources_reasons_and_formula():
    html = render_html(make_report())
    assert "zakupki.gov.ru/x?1" in html
    assert "цена выше медианы в 3,7 раза" in html
    assert "НМЦД = 2&nbsp;500 ×" in html       # формула расчёта позиции, числа без переноса строки
    assert "ООО «Поставщик 1»" in html
    assert "Скачать обоснование" not in html  # без кнопок выгрузки, если их не передали


def test_xlsx_has_formulas():
    wb = load_workbook(io.BytesIO(render_xlsx(make_report())))
    ws = wb["Расчёт"]
    formulas = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert any("AVERAGE(" in f for f in formulas)
    # STDEV (выборочное, n−1) понимают все версии Excel и LibreOffice; STDEV.S без префикса _xlfn даёт #NAME?
    assert any("STDEV(" in f for f in formulas) and not any("STDEV.S(" in f for f in formulas)
    assert "='Расчёт'!" in " ".join(str(c.value) for row in wb["Сводка"].iter_rows() for c in row)


def test_docx_clean_properties():
    data = render_docx(make_report(), "МФТИ")
    z = zipfile.ZipFile(io.BytesIO(data))
    core = z.read("docProps/core.xml").decode()
    app = z.read("docProps/app.xml").decode()
    assert "python-docx" not in core and "python-docx" not in app
    assert app.count("<Company>") == 1 and "<Company>МФТИ</Company>" in app
    body = z.read("word/document.xml").decode()
    assert "Обоснование начальной (максимальной) цены договора" in body
    assert "рубл" in body and "копе" in body


def test_kp_request():
    z = zipfile.ZipFile(io.BytesIO(render_kp_docx(make_report(), "МФТИ")))
    body = z.read("word/document.xml").decode()
    assert "Запрос ценовой информации" in body and "ООО «Поставщик 1»" in body


def test_docx_price_sources_follow_the_run():
    """В документе обоснования перечислены только те источники цен, что были в этом расчёте."""
    from docx import Document
    rep = make_report()
    text = "\n".join(p.text for p in Document(io.BytesIO(render_docx(rep, "Заказчик"))).paragraphs)
    line = next(ln for ln in text.splitlines() if ln.startswith("Источники ценовой информации"))
    assert "реестр контрактов ЕИС" in line and "сайты поставщиков" not in line and "коммерческие" not in line
    rep.settings["web"] = True
    text = "\n".join(p.text for p in Document(io.BytesIO(render_docx(rep, "Заказчик"))).paragraphs)
    assert "сайты поставщиков (публичные предложения)" in text
