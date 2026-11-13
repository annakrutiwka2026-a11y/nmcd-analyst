"""ЕИС: реестр контрактов по 44-ФЗ (zakupki.gov.ru).

Берём только исполненные контракты («Исполнение завершено»). Из карточки контракта
забираем позиции с ценой за единицу, КТРУ/ОКПД2, количеством и полным списком
характеристик, а также поставщика (наименование и ИНН) и дату заключения.
Для цен, прошедших сверку, проверяются неустойки по контракту (вкладка «Исполнение»)
и запись поставщика в реестре недобросовестных поставщиков.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from lxml import html as LH

from app.models import Evidence
from app.sources.http import FetchResult, HttpClient

BASE = "https://zakupki.gov.ru"


def _txt(node) -> str:
    return " ".join(" ".join(node.xpath(".//text()")).split())


def _money(text: str) -> Optional[Decimal]:
    m = re.search(r"\d[\d\s ]*(?:[.,]\d+)?", text or "")
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _date(text: str) -> Optional[datetime]:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class EisPosition:
    index: int
    name: str
    chars_text: str
    chars: list[tuple[str, str]]
    ktru: Optional[str]
    okpd2: Optional[str]
    quantity: Optional[Decimal]
    unit: str
    unit_price: Optional[Decimal]
    total: Optional[Decimal]
    vat: Optional[str]
    chars_url: Optional[str]


@dataclass
class EisContract:
    reestr: str
    url: str
    objects_url: str
    customer: str = ""
    supplier_name: Optional[str] = None
    supplier_inn: Optional[str] = None
    signed_at: Optional[datetime] = None
    status: str = ""
    method: str = ""
    subject: str = ""
    price: Optional[Decimal] = None
    positions: list[EisPosition] = field(default_factory=list)
    evidence: Optional[Evidence] = None
    objects_evidence: Optional[Evidence] = None


@dataclass
class EisHit:
    reestr: str
    status: str
    customer: str
    objects: str
    price: Optional[Decimal]
    signed_at: Optional[datetime]


def parse_search(html_text: str) -> list[EisHit]:
    """Разбор страницы результатов поиска по реестру контрактов."""
    doc = LH.fromstring(html_text)
    hits: list[EisHit] = []
    for block in doc.xpath("//div[contains(@class,'search-registry-entry-block')]"):
        link = block.xpath(".//a[contains(@href,'common-info.html?reestrNumber=')]/@href")
        if not link:
            continue
        m = re.search(r"reestrNumber=(\d+)", link[0])
        if not m:
            continue
        text = _txt(block)
        status = ""
        sm = re.search(r"(Исполнение завершено|Исполнение прекращено|Исполнение|Аннулировано)", text)
        if sm:
            status = sm.group(1)
        cust = ""
        cm = re.search(r"Заказчик\s+(.+?)\s+Контракт №", text)
        if cm:
            cust = cm.group(1)
        objs = ""
        om = re.search(r"Объекты закупки\s+(.+?)\s+Цена контракта", text)
        if om:
            objs = om.group(1)
        price = None
        pm = re.search(r"Цена контракта\s+([\d\s ,]+)", text)
        if pm:
            price = _money(pm.group(1))
        signed = None
        dm = re.search(r"Заключение контракта\s+(\d{2}\.\d{2}\.\d{4})", text)
        if dm:
            signed = _date(dm.group(1))
        hits.append(EisHit(m.group(1), status, cust, objs, price, signed))
    return hits


def _labelled(doc, label: str) -> str:
    """Значение поля карточки по подписи (section__title → section__info)."""
    for node in doc.xpath(f"//*[contains(@class,'section__title') and normalize-space()='{label}']"):
        sib = node.getnext()
        if sib is not None:
            return _txt(sib)
    return ""


def parse_common(html_text: str, contract: EisContract) -> None:
    doc = LH.fromstring(html_text)
    contract.status = _labelled(doc, "Статус контракта") or contract.status
    contract.signed_at = _date(_labelled(doc, "Дата заключения контракта")) or contract.signed_at
    contract.method = _labelled(doc, "Способ определения поставщика (подрядчика, исполнителя)")
    contract.subject = _labelled(doc, "Предмет контракта")
    price = _money(_labelled(doc, "Цена контракта"))
    contract.price = price or contract.price
    full = _txt(doc)
    i = full.find("Информация о поставщиках")
    if i >= 0:
        m = re.search(r"ИНН:\s*(\d{10,12})", full[i:i + 4000])
        if m:
            seg = full[i + len("Информация о поставщиках"): i + m.start()]
            for label in ("Телефон, электронная почта", "Почтовый адрес", "Адрес места нахождения", "Страна, код",
                          "Организация", "Статус"):
                j = seg.rfind(label)
                if j >= 0:
                    seg = seg[j + len(label):] if label in ("Телефон, электронная почта", "Статус") else seg
            name = re.sub(r"\s+(Индивидуальный предприниматель|Юридическое лицо.*|Физическое лицо.*)$", "",
                          seg.strip()).strip()
            contract.supplier_name = name[:200] or None
            contract.supplier_inn = m.group(1)
    if not contract.customer:
        cm = re.search(r"Полное наименование заказчика\s+(.+?)\s+(?:Сокращенное|Идентификационный|ИНН)", full)
        if cm:
            contract.customer = cm.group(1)[:300]


def parse_objects(html_text: str) -> list[EisPosition]:
    doc = LH.fromstring(html_text)
    out: list[EisPosition] = []
    for tbl in doc.xpath("//table"):
        head = " ".join(tbl.xpath(".//thead//text()"))
        if "Цена за единицу" not in head:
            continue
        for idx, tr in enumerate(tbl.xpath("./tbody/tr")):
            tds = tr.xpath("./td")
            if len(tds) < 7:
                continue
            divs = tds[1].xpath("./div")
            name = _txt(divs[0]) if divs else _txt(tds[1])
            name = re.sub(r"^\d+\.\s*", "", name)
            chars_text = ""
            chars_url = None
            for dv in divs[1:]:
                t = _txt(dv).replace("Все характеристики ТРУ", "").strip()
                if t.startswith("Страна происхождения"):
                    continue
                chars_text = (chars_text + " " + t).strip()
                href = dv.xpath(".//a[contains(@href,'ktru-characteristics')]/@href")
                if href:
                    chars_url = BASE + href[0]
            codes = _txt(tds[2])
            ktru = None
            okpd2 = None
            km = re.search(r"\(\s*(\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{8})\s*\)", codes)
            if km:
                ktru = km.group(1)
            om = re.findall(r"\(\s*(\d{2}\.\d{2}(?:\.\d{1,2}(?:\.\d{3})?)?)\s*\)", codes)
            if om:
                okpd2 = om[-1]
            qty_cell = tds[4]
            qty_text = _txt(qty_cell)
            tooltip = qty_cell.xpath(".//@data-tooltip")
            unit = ""
            if tooltip:
                unit = re.sub(r"<[^>]+>", "", tooltip[0]).strip()
            qm = re.match(r"([\d\s ,\.]+)\s*(.*)", qty_text)
            quantity = _money(qm.group(1)) if qm else None
            if not unit and qm:
                unit = qm.group(2).strip()
            unit_price = _money(_txt(tds[5]))
            total_text = _txt(tds[6])
            total = _money(total_text)
            vat = None
            vm = re.search(r"Ставка НДС:\s*([^\s].*)$", total_text)
            if vm:
                vat = vm.group(1).strip()
            out.append(EisPosition(idx + 1, name, chars_text, [], ktru, okpd2, quantity, unit, unit_price, total,
                                   vat, chars_url))
        break
    return out


def parse_chars(html_text: str) -> list[tuple[str, str]]:
    """Полный список характеристик позиции КТРУ: пары (название, значение)."""
    doc = LH.fromstring(html_text)
    pairs: list[tuple[str, str]] = []
    for sec in doc.xpath("//div[contains(@class,'blockInfo__section')]"):
        title = sec.xpath("./*[contains(@class,'section__title')]")
        info = sec.xpath("./*[contains(@class,'section__info')]")
        if title and info:
            name, value = _txt(title[0]), _txt(info[0])
            if name and value and len(name) < 300:
                pairs.append((name, value))
    if not pairs:
        for tr in doc.xpath("//table//tr"):
            cells = [c for c in (_txt(td) for td in tr.xpath("./td")) if c]
            if len(cells) >= 2 and len(cells[0]) < 300:
                pairs.append((cells[0], " ".join(cells[1:])))
    return pairs


def parse_penalties(html_text: str) -> Optional[bool]:
    """Неустойки по контракту с вкладки «Исполнение (расторжение) контракта»: колонка «Неустойки (штрафы, пени)»
    по этапам. True — хотя бы в одном этапе неустойка указана, False — во всех этапах «Нет», None — данных нет."""
    doc = LH.fromstring(html_text)
    seen_no = False
    for tbl in doc.xpath("//table"):
        heads = [_txt(th) for th in tbl.xpath(".//thead//th")]
        col = next((i for i, h in enumerate(heads) if h.startswith("Неустойки")), None)
        if col is None:
            continue
        for tr in tbl.xpath(".//tr[td]"):
            tds = tr.xpath("./td")
            if len(tds) <= col:
                continue
            cell = _txt(tds[col]).strip().lower()
            if not cell:
                continue
            if cell in ("нет", "-", "—"):
                seen_no = True
                continue
            return True
    return False if seen_no else None


def parse_rnp(html_text: str, inn: str) -> bool:
    """Есть ли действующая запись («Размещено») о поставщике с этим ИНН в реестре недобросовестных поставщиков."""
    doc = LH.fromstring(html_text)
    for block in doc.xpath("//div[contains(@class,'search-registry-entry-block')]"):
        text = _txt(block)
        if re.search(r"ИНН[^0-9]{0,40}" + re.escape(inn) + r"(?!\d)", text) and "Размещено" in text[:80]:
            return True
    return False


class EisSource:
    kind = "eis44"

    def __init__(self, http: HttpClient, config: dict):
        self.http = http
        self.cfg = config
        self.interval = float(config.get("min_interval_sec", 0.6))
        self._contracts: dict[str, Optional[EisContract]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._chars: dict[str, list[tuple[str, str]]] = {}
        self._penalties: dict[str, Optional[bool]] = {}
        self._rnp: dict[str, Optional[bool]] = {}

    async def penalties(self, reestr: str) -> tuple[Optional[bool], str]:
        """Взыскивались ли неустойки по контракту (ч. 18 ст. 22 44-ФЗ) и адрес страницы для проверки."""
        url = f"{BASE}/epz/contract/contractCard/process-info.html?reestrNumber={reestr}"
        if reestr not in self._penalties:
            res = await self._get("/epz/contract/contractCard/process-info.html", {"reestrNumber": reestr},
                                  evidence=False)
            self._penalties[reestr] = parse_penalties(res.text) if res.ok else None
        return self._penalties[reestr], url

    async def in_rnp(self, inn: str) -> tuple[Optional[bool], str]:
        """Включён ли поставщик в реестр недобросовестных поставщиков (п. 3.13.1), по ИНН; действующие записи
        по 44-ФЗ и 223-ФЗ."""
        params = {"searchString": inn, "fz44": "on", "fz223": "on", "dsStatuses": "0", "dsStatuses_0": "on",
                  "recordsPerPage": "_10"}
        url = (f"{BASE}/epz/dishonestsupplier/search/results.html?searchString={inn}&fz44=on&fz223=on"
               f"&dsStatuses=0&dsStatuses_0=on")
        if inn not in self._rnp:
            res = await self._get("/epz/dishonestsupplier/search/results.html", params, evidence=False)
            self._rnp[inn] = parse_rnp(res.text, inn) if res.ok else None
        return self._rnp[inn], url

    async def _get(self, path: str, params: Optional[dict] = None, evidence: bool = True) -> FetchResult:
        return await self.http.get(BASE + path, params=params, min_interval=self.interval, save_evidence=evidence)

    async def search(self, query: str) -> tuple[list[EisHit], Optional[str], str]:
        params = {
            "searchString": query, "morphology": "on", "fz44": "on",
            f"contractStageList_{self.cfg.get('stage', '1')}": "on", "contractStageList": self.cfg.get("stage", "1"),
            "sortBy": "UPDATE_DATE", "sortDirection": "false",
            "recordsPerPage": f"_{int(self.cfg.get('results_per_query', 20))}", "pageNumber": "1",
        }
        res = await self._get("/epz/contract/search/results.html", params, evidence=False)
        if not res.ok:
            return [], res.error, res.url
        return parse_search(res.text), None, res.url

    async def contract(self, reestr: str) -> Optional[EisContract]:
        lock = self._locks.setdefault(reestr, asyncio.Lock())
        async with lock:
            if reestr in self._contracts:
                return self._contracts[reestr]
            url = f"{BASE}/epz/contract/contractCard/common-info.html?reestrNumber={reestr}"
            objects_url = f"{BASE}/epz/contract/contractCard/payment-info-and-target-of-order.html?reestrNumber={reestr}"
            c = EisContract(reestr=reestr, url=url, objects_url=objects_url)
            common = await self._get("/epz/contract/contractCard/common-info.html", {"reestrNumber": reestr})
            objects = await self._get("/epz/contract/contractCard/payment-info-and-target-of-order.html",
                                      {"reestrNumber": reestr})
            if not objects.ok:
                self._contracts[reestr] = None
                return None
            if common.ok:
                parse_common(common.text, c)
                c.evidence = common.evidence
            c.positions = parse_objects(objects.text)
            c.objects_evidence = objects.evidence
            self._contracts[reestr] = c
            return c

    async def full_chars(self, pos: EisPosition) -> list[tuple[str, str]]:
        if not pos.chars_url:
            return []
        if pos.chars_url in self._chars:
            return self._chars[pos.chars_url]
        res = await self.http.get(pos.chars_url, min_interval=self.interval, save_evidence=False)
        pairs = parse_chars(res.text) if res.ok else []
        self._chars[pos.chars_url] = pairs
        return pairs
