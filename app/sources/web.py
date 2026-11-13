"""Сайты поставщиков: поиск через Яндекс Search API v2 и разбор карточек товаров.

Страницы скачиваются только если это разрешает robots.txt. Сайты с антибот-защитой
пропускаются и попадают в отчёт с причиной. Цена берётся из разметки schema.org
(JSON-LD, микроразметка), а характеристики и спорные цены — из текстовых фрагментов.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlsplit

from lxml import etree
from lxml import html as LH

from app.config import Settings
from app.models import Evidence
from app.sources.http import HttpClient, domain_matches, host_of

_PRICE_RX = re.compile(r"(\d{1,3}(?:[\s  ]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s*(?:₽|руб\.?|р\.(?!\w)|RUB)", re.I)
_INN_RX = re.compile(r"ИНН\s*[:№]?\s*(\d{10}|\d{12})(?!\d)")
_OGRN_RX = re.compile(r"ОГРН(?:ИП)?\s*[:№]?\s*(\d{13}|\d{15})(?!\d)")
_ORG_RX = re.compile(r"((?:ООО|АО|ПАО|ЗАО|ОАО|ИП)\s*[«\"][^»\"]{2,80}[»\"]|ИП\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)")
_PHONE_RX = re.compile(r"(?:\+7|8)[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_EMAIL_RX = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")


def inn_valid(inn: str) -> bool:
    """Проверка контрольных цифр ИНН."""
    d = [int(c) for c in inn]
    if len(d) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        return d[9] == sum(a * b for a, b in zip(w, d)) % 11 % 10
    if len(d) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        return (d[10] == sum(a * b for a, b in zip(w1, d)) % 11 % 10
                and d[11] == sum(a * b for a, b in zip(w2, d)) % 11 % 10)
    return False


@dataclass
class WebHit:
    url: str
    domain: str
    title: str
    snippet: str
    query: str


@dataclass
class WebPage:
    url: str
    domain: str
    title: str = ""
    ok: bool = False
    error: str = ""
    jsonld: list[dict[str, Any]] = field(default_factory=list)
    prices: list[tuple[Decimal, str]] = field(default_factory=list)   # (цена, контекст)
    structured_price: Optional[Decimal] = None
    structured_name: str = ""
    structured_brand: str = ""
    seller_name: Optional[str] = None
    seller_inn: Optional[str] = None
    specs: list[tuple[str, str]] = field(default_factory=list)   # характеристики из таблиц страницы
    text: str = ""
    evidence: Optional[Evidence] = None


def _num(text: str) -> Optional[Decimal]:
    try:
        return Decimal(str(text).replace(" ", "").replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def parse_search_xml(raw: str) -> list[tuple[str, str, str, str]]:
    """Разбор XML-ответа поиска: (url, domain, title, snippet)."""
    out: list[tuple[str, str, str, str]] = []
    try:
        root = etree.fromstring(raw.encode("utf-8"))
    except etree.XMLSyntaxError:
        return out
    for doc in root.iter("doc"):
        url = (doc.findtext("url") or "").strip()
        domain = (doc.findtext("domain") or host_of(url)).strip().lower()
        title_el = doc.find("title")
        title = " ".join("".join(title_el.itertext()).split()) if title_el is not None else ""
        passages = [" ".join("".join(p.itertext()).split()) for p in doc.iter("passage")]
        out.append((url, domain, title, " … ".join(passages)[:600]))
    return out


def _walk_jsonld(node: Any, acc: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for x in node:
            _walk_jsonld(x, acc)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t in ("Product", "IndividualProduct", "ProductModel") for t in types if isinstance(t, str)):
            acc.append(node)
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                _walk_jsonld(node[key], acc)


def _offer_price(product: dict[str, Any]) -> Optional[Decimal]:
    offers = product.get("offers")
    candidates = offers if isinstance(offers, list) else [offers]
    for off in candidates:
        if not isinstance(off, dict):
            continue
        cur = str(off.get("priceCurrency") or "RUB").upper()
        if cur not in ("RUB", "RUR", "₽"):
            continue
        for key in ("price", "lowPrice"):
            val = off.get(key)
            if val not in (None, ""):
                p = _num(str(val))
                if p and p > 0:
                    return p
    return None


_SPEC_CLASS = re.compile(r"char|spec|prop|param|attr|feature|харак|detail|tech", re.I)


def extract_specs(doc, limit: int = 60) -> list[tuple[str, str]]:
    """Пары «характеристика — значение» из таблиц, списков определений и блоков характеристик карточки товара."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, value: str) -> None:
        name = " ".join((name or "").split()).strip(" :—–-")
        value = " ".join((value or "").split()).strip()
        if not name or not value or len(name) > 60 or len(value) > 160 or name == value:
            return
        if not re.search(r"[A-Za-zА-Яа-яЁё]", name) or name.lower() in ("цена", "price", "стоимость"):
            return
        key = (name.lower(), value.lower())
        if key not in seen:
            seen.add(key)
            out.append((name, value))

    for tr in doc.xpath("//table//tr"):
        cells = [c for c in tr if c.tag in ("td", "th")]
        if len(cells) == 2:
            add(cells[0].text_content(), cells[1].text_content())
    for dl in doc.xpath("//dl"):
        for dt, dd in zip(dl.xpath("./dt"), dl.xpath("./dd")):
            add(dt.text_content(), dd.text_content())
    for el in doc.xpath("//*[@class]"):
        if len(out) >= limit:
            break
        if el.tag in ("script", "style", "template") or not _SPEC_CLASS.search(el.get("class") or ""):
            continue
        text = " ".join(el.text_content().split())
        if not 3 < len(text) < 220:
            continue
        kids = [k for k in el if isinstance(k.tag, str) and k.text_content().strip()]
        if len(kids) == 2:
            add(kids[0].text_content(), kids[1].text_content())
        elif text.count(":") == 1:
            a, b = text.split(":", 1)
            add(a, b)
    return out[:limit]


def visible_text(doc) -> str:
    for bad in doc.xpath("//script|//style|//noscript|//svg|//template|//iframe"):
        bad.drop_tree()
    for hidden in doc.xpath("//*[@hidden or @aria-hidden='true' or contains(translate(@style,' ',''),'display:none') "
                            "or contains(translate(@style,' ',''),'visibility:hidden')]"):
        hidden.drop_tree()
    text = " ".join(doc.text_content().split())
    return text


def extract_page(url: str, html_text: str) -> WebPage:
    page = WebPage(url=url, domain=host_of(url))
    try:
        doc = LH.fromstring(html_text)
    except (etree.ParserError, ValueError):
        page.error = "страница не разбирается как HTML"
        return page
    title = doc.xpath("//meta[@property='og:title']/@content") or doc.xpath("//h1//text()") or doc.xpath("//title/text()")
    page.title = " ".join(" ".join(title[:3] if title and not isinstance(title[0], str) else title[:1]).split())[:300]
    products: list[dict[str, Any]] = []
    for raw in doc.xpath("//script[@type='application/ld+json']/text()"):
        try:
            _walk_jsonld(json.loads(raw.strip()), products)
        except (ValueError, TypeError):
            continue
    page.jsonld = products[:5]
    if products:
        main = products[0]
        page.structured_price = _offer_price(main)
        page.structured_name = str(main.get("name") or "")[:300]
        brand = main.get("brand")
        page.structured_brand = str((brand.get("name") if isinstance(brand, dict) else brand) or "")[:100]
    if page.structured_price is None:
        meta = (doc.xpath("//meta[@property='product:price:amount']/@content")
                or doc.xpath("//meta[@itemprop='price']/@content")
                or doc.xpath("//*[@itemprop='price']/@content"))
        if meta:
            p = _num(meta[0])
            if p and p > 0:
                page.structured_price = p
    page.specs = extract_specs(doc)
    text = visible_text(doc)
    for m in _PRICE_RX.finditer(text):
        whole = m.group(1).replace(" ", "").replace(" ", "").replace(" ", "")
        val = _num(whole + ("." + m.group(2) if m.group(2) else ""))
        if val and Decimal("1") <= val <= Decimal("100000000"):
            ctx = text[max(0, m.start() - 80): m.end() + 40]
            page.prices.append((val, ctx))
        if len(page.prices) >= 30:
            break
    for m in _INN_RX.finditer(text):
        if inn_valid(m.group(1)):
            page.seller_inn = m.group(1)
            break
    om = _ORG_RX.search(text)
    if om:
        page.seller_name = om.group(1)[:120]
    page.text = text
    shell = re.fullmatch(r"\s*загрузка\.*\s*", (page.title or "").lower()) is not None
    if shell or (len(text) < 250 and not products and page.structured_price is None):
        page.error = "страница формируется скриптом в браузере, текста товара в ней нет"
        return page
    page.ok = True
    return page


def fragment_for_llm(page: WebPage, keywords: list[str], limit: int = 5000) -> str:
    """Короткий фрагмент страницы для модели: заголовок, разметка товара и окна вокруг цен и ключевых слов.
    Телефоны, e-mail и ИНН вырезаются: модели они не нужны."""
    text = page.text
    windows: list[tuple[int, int]] = []
    lowered = text.lower()
    for kw in keywords:
        kw = kw.lower().strip()
        if len(kw) < 3:
            continue
        start = 0
        hits = 0
        while hits < 3:
            i = lowered.find(kw, start)
            if i < 0:
                break
            windows.append((max(0, i - 250), min(len(text), i + 450)))
            start = i + len(kw)
            hits += 1
    for _, ctx in page.prices[:6]:
        i = text.find(ctx)
        if i >= 0:
            windows.append((max(0, i - 200), min(len(text), i + len(ctx) + 200)))
    windows.sort()
    merged: list[list[int]] = []
    for a, b in windows:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    parts = [text[a:b] for a, b in merged]
    head = f"Заголовок: {page.title}\n"
    if page.structured_name or page.structured_price:
        head += f"Разметка товара: {page.structured_name}; бренд: {page.structured_brand}; цена: {page.structured_price}\n"
    if page.specs:
        head += "Характеристики: " + "; ".join(f"{a}: {b}" for a, b in page.specs)[:1500] + "\n"
    body = " … ".join(parts) if parts else text[:limit]
    body = _PHONE_RX.sub("[телефон]", _EMAIL_RX.sub("[e-mail]", _INN_RX.sub("ИНН [скрыт]", body)))
    return (head + body)[:limit]


class WebSource:
    kind = "web"

    def __init__(self, http: HttpClient, settings: Settings, cfg: dict):
        self.http = http
        self.settings = settings
        self.cfg = cfg
        self.blocked = list(cfg.get("blocked_domains", []))
        self.skipped = list(cfg.get("skip_domains", []))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and self.settings.web_enabled

    async def search(self, query: str) -> tuple[list[WebHit], Optional[str]]:
        payload = {
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
            "responseFormat": "FORMAT_XML",
            "groupSpec": {"groupsOnPage": int(self.cfg.get("results_per_query", 10))},
            "folderId": self.settings.yandex_folder_id,
        }
        headers = {"Authorization": f"Api-Key {self.settings.yandex_api_key}"}
        data, err = await self.http.post_json(self.cfg.get("endpoint"), payload, headers)
        if err or not data:
            return [], err or "пустой ответ"
        raw = data.get("rawData")
        if not raw:
            return [], "в ответе нет результатов"
        xml = base64.b64decode(raw).decode("utf-8", "replace")
        hits = []
        for url, domain, title, snippet in parse_search_xml(xml):
            hits.append(WebHit(url=url, domain=domain, title=title, snippet=snippet, query=query))
        return hits, None

    def usable(self, hit: WebHit) -> tuple[bool, str]:
        host = host_of(hit.url) or hit.domain
        if not hit.url.startswith(("http://", "https://")):
            return False, "не веб-страница"
        if domain_matches(host, self.blocked):
            return False, "сайт с антибот-защитой или запретом для программ, обход не выполняется"
        if domain_matches(host, self.skipped):
            return False, "агрегатор тендеров или справочный сайт, а не предложение поставщика"
        if urlsplit(hit.url).path.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")):
            return False, "файл, а не страница товара"
        return True, ""

    async def fetch(self, hit: WebHit) -> WebPage:
        res = await self.http.get(hit.url, min_interval=float(self.cfg.get("min_interval_per_host_sec", 1.0)))
        if not res.ok:
            return WebPage(url=hit.url, domain=hit.domain, error=res.error or f"HTTP {res.status}")
        if "html" not in (res.content_type or "html"):
            return WebPage(url=hit.url, domain=hit.domain, error="не HTML-страница")
        try:
            page = extract_page(res.final_url, res.text)
        except Exception as exc:  # одна нестандартная страница не должна останавливать расчёт
            return WebPage(url=hit.url, domain=hit.domain, error=f"страница не разобрана ({type(exc).__name__})",
                           evidence=res.evidence)
        page.evidence = res.evidence
        return page
