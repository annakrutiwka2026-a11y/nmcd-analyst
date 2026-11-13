"""Конвейер обоснования НМЦД: разбор ТЗ → поиск предложений → сверка → приведение → отбор и расчёт → отчёт.

Каждый шаг пишет событие в журнал отчёта, поэтому путь от ТЗ до цены можно восстановить целиком.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from app.config import Settings
from app.engine.cpi import CpiSeries
from app.engine.matching import apply_card_rules, match_offer, name_similarity
from app.engine.normalize import normalize_offer
from app.engine.rules import Rules, load_rules
from app.engine.selection import decide_and_calculate
from app.engine.units import canon_unit, dimension, parse_quantity, split_value_unit
from app.ingest.reader import Document
from app.ingest.rules_parser import parse_procurement
from app.llm.client import LLMClient
from app.llm.prompts import CARD_SCHEMA, CARD_SYSTEM, OFFERS_SCHEMA, OFFERS_SYSTEM, card_user, offers_user
from app.llm.redact import redact
from app.models import (CalcResult, CalcStatus, DecisionCode, Item, ItemResult, MatchClass, MatchResult, Offer,
                        OfferChar, Op, Procurement, Quantity, Report, Requirement, SourceKind, SourceStatus,
                        TraceEvent)
from app.sources.eis import EisContract, EisPosition, EisSource
from app.sources.http import HttpClient
from app.sources.web import WebHit, WebSource, fragment_for_llm
from app.textutil import norm_text as _norm_text, quote_found, terms as _terms

ProgressFn = Callable[[TraceEvent], Optional[Awaitable[None]]]

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _num(value: Any) -> Optional[Decimal]:
    """Число из ответа модели («1 200,50», 1200.5); None — если это не число или оно не больше нуля."""
    if value is None or isinstance(value, bool):
        return None
    text = re.sub(r"[\s\u00a0\u202f]", "", str(value)).replace(",", ".")
    m = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    d = Decimal(text)
    return d if d > 0 else None


class Run:
    """Один прогон анализа: состояние, журнал и вызовы источников."""

    def __init__(self, settings: Settings, run_id: Optional[str] = None, progress: Optional[ProgressFn] = None,
                 use_llm: Optional[bool] = None, use_web: Optional[bool] = None):
        self.settings = settings
        self.id = run_id or uuid.uuid4().hex[:12]
        self.progress = progress
        self.rules: Rules = load_rules()
        self.cpi = CpiSeries.load()
        self.llm = LLMClient(settings)
        if use_llm is False:
            self.llm.enabled = False
        self.use_web = settings.web_enabled if use_web is None else (use_web and settings.web_enabled)
        self.calc_date = date.today()
        self.report: Optional[Report] = None

    async def log(self, stage: str, message: str, **data: Any) -> None:
        ev = TraceEvent(ts=_now(), stage=stage, message=message, data=data)
        if self.report is not None:
            self.report.trace.append(ev)
        if self.progress is not None:
            res = self.progress(ev)
            if asyncio.iscoroutine(res):
                await res

    # ------------------------------------------------------------------ карточка

    async def build_card(self, doc: Document) -> Procurement:
        proc = parse_procurement(doc)
        await self.log("parse", f"Документ «{doc.name}» разобран по правилам: позиций {len(proc.items)}",
                       items=[it.name for it in proc.items])
        if not self.llm.enabled:
            proc.notes.append("Модель не подключена: карточка составлена по правилам.")
            return proc
        red = redact(doc.text)
        self.report.settings["redaction"] = {"summary": red.summary(), "counts": red.counts,
                                             "preview": red.text[:1500]}
        await self.log("privacy", f"Перед отправкой во внешнюю модель обезличено: {red.summary()}", counts=red.counts)
        if red.leaks:
            await self.log("privacy", "Проверка утечки не пройдена, модель для ТЗ не вызывается", leaks=red.leaks)
            return proc
        data = await self.llm.tool_json(system=CARD_SYSTEM, user=card_user(red.text, proc), tool_name="save_card",
                                        tool_description="Сохранить карточки позиций закупки", schema=CARD_SCHEMA,
                                        purpose="карточка ТЗ (обезличенный текст)", max_tokens=12000)
        if not data or not data.get("items"):
            await self.log("parse", "Модель не вернула карточку, используется разбор по правилам")
            return proc
        merged = self._merge_card(proc, data, doc.text, red.text)
        await self.log("parse", "Карточка уточнена моделью: существенные характеристики, фасовка, поисковые фразы",
                       items=[{"name": it.name, "requirements": len(it.requirements),
                               "mandatory": sum(r.mandatory for r in it.requirements)} for it in merged.items])
        return merged

    def _merge_card(self, proc: Procurement, data: dict[str, Any], text: str, red_text: str) -> Procurement:
        by_num = {int(x.get("number") or 0): x for x in data.get("items", [])}
        items: list[Item] = []
        base_items = proc.items
        if len(base_items) == 1 and len(by_num) > 1 and proc.notes:
            base_items = []
            for n, x in sorted(by_num.items()):
                base_items.append(Item(id=f"item{n}", number=n, name=x.get("name") or f"Позиция {n}",
                                       quantity=_num(x.get("quantity")) or Decimal(1), unit=x.get("unit") or "шт."))
        for it in base_items:
            x = by_num.get(it.number)
            if not x:
                items.append(it)
                continue
            reqs: list[Requirement] = []
            for k, r in enumerate(x.get("requirements") or [], start=1):
                try:
                    op = Op(r.get("op") or "contains")
                except ValueError:
                    op = Op.CONTAINS
                quote = (r.get("quote") or "")[:300]
                verified = quote_found(quote, text) or quote_found(quote, red_text)
                reqs.append(Requirement(
                    id=f"{it.id}_r{k}", name=(r.get("name") or "")[:200], op=op,
                    value=None if r.get("value") is None else str(r.get("value")),
                    value_max=None if r.get("value_max") is None else str(r.get("value_max")),
                    values=[str(v) for v in (r.get("values") or [])], unit=r.get("unit"),
                    tolerance=r.get("tolerance"), mandatory=bool(r.get("mandatory")) and verified,
                    by_brand=bool(r.get("by_brand")) and bool(x.get("brand") or x.get("model")), quote=quote,
                ))
            content, content_op = it.content, it.content_op
            if _num(x.get("content_value")) and x.get("content_unit"):
                content = Quantity(value=_num(x["content_value"]), unit=str(x["content_unit"]))
                content_op = Op.GE if x.get("content_op") == ">=" else Op.EQ
            items.append(it.model_copy(update={
                "short_name": x.get("short_name") or it.short_name,
                "brand": x.get("brand") or it.brand,
                "model": x.get("model") or it.model,
                "content": content, "content_op": content_op,
                "requirements": reqs or it.requirements,
                "eis_queries": [q for q in (x.get("eis_queries") or []) if q][:3] or it.eis_queries,
                "web_queries": [q for q in (x.get("web_queries") or []) if q][:3] or it.web_queries,
                "negative_terms": [q for q in (x.get("negative_terms") or []) if q][:6],
            }))
        items = [apply_card_rules(it) for it in items]
        return proc.model_copy(update={"items": items, "parse_mode": "llm",
                                       "title": proc.title if proc.title != "Закупка" else (data.get("title") or proc.title)})

    # ------------------------------------------------------------------ ЕИС

    def _position_score(self, item: Item, pos: EisPosition) -> float:
        text = f"{pos.name} {pos.chars_text}"
        pos_terms = _terms(text)
        keys = set()
        for q in [item.short_name or item.name] + item.eis_queries:
            keys |= _terms(q)
        if not keys:
            return 0.0
        hit = len(keys & pos_terms)
        score = hit / len(keys)
        if any(_norm_text(n) in _norm_text(text) for n in item.negative_terms if len(n) > 3):
            score *= 0.2
        if item.brand and _norm_text(item.brand) in _norm_text(text):
            score += 0.3
        if item.model and _norm_text(item.model).replace(" ", "") in _norm_text(text).replace(" ", ""):
            score += 0.3
        low = f" {_norm_text(text)} "
        for r in item.requirements:
            v = _norm_text(r.value or "")
            if r.mandatory and v and len(v) <= 12 and re.search(r"\d", v) and f" {v} " in low:
                score += 0.15
        bigrams = {k for k in keys if " " in k}
        if bigrams and not (bigrams & pos_terms) and score < 0.5:
            score *= 0.5
        return score

    async def collect_eis(self, item: Item, eis: EisSource, status: SourceStatus) -> list[Offer]:
        cfg = eis.cfg
        cutoff = _now() - timedelta(days=int(self.rules.method.max_age_months * 30.44))
        per_query: list[list[Any]] = []
        for q in (item.eis_queries or [item.short_name or item.name])[: int(cfg.get("max_queries_per_item", 3))]:
            hits, err, url = await eis.search(q)
            status.requests += 1
            if err:
                status.errors += 1
                await self.log("search", f"ЕИС: запрос «{q}» не выполнен: {err}", item=item.id, source="eis44")
                continue
            fresh = [h for h in hits if h.status == "Исполнение завершено" and (h.signed_at is None or h.signed_at >= cutoff)]
            per_query.append(fresh)
            await self.log("search", f"ЕИС: «{q}» — найдено контрактов {len(hits)}, исполненных за 3 года {len(fresh)}",
                           item=item.id, source="eis44", url=url)
        seen: dict[str, Any] = {}
        for rank in range(max((len(x) for x in per_query), default=0)):   # по очереди из каждого запроса
            for fresh in per_query:
                if rank < len(fresh):
                    seen.setdefault(fresh[rank].reestr, fresh[rank])
        offers: list[Offer] = []
        candidates: list[tuple[float, EisContract, EisPosition]] = []
        for reestr in list(seen)[: int(cfg.get("max_contracts_per_item", 14))]:
            c = await eis.contract(reestr)
            status.requests += 2
            if c is None:
                status.errors += 1
                continue
            for pos in c.positions:
                s = self._position_score(item, pos)
                if s >= 0.34 and pos.unit_price:
                    candidates.append((s, c, pos))
        candidates.sort(key=lambda t: (-t[0], t[1].reestr, t[2].index))
        for n, (score, c, pos) in enumerate(candidates[:16], start=1):
            pairs = await eis.full_chars(pos)
            norm = [(a, *split_value_unit(a, b), b) for a, b in pairs]
            chars_line = ("; ".join(f"{a}: {v}{' ' + u if u else ''}" for a, v, u, _ in norm) if norm
                          else pos.chars_text)
            text = (f"{pos.name}. Характеристики: {chars_line}. Единица измерения: {pos.unit}. "
                    f"Цена за единицу: {pos.unit_price} руб. Количество в контракте: {pos.quantity}.")
            content = None
            unit_c = canon_unit(pos.unit)
            if unit_c and dimension(unit_c) in ("mass", "volume", "sheet"):
                content = Quantity(value=Decimal("1"), unit=unit_c)
            offers.append(Offer(
                id=f"{item.id}-e{n}", item_id=item.id, source=SourceKind.EIS44,
                source_name="ЕИС, реестр контрактов 44-ФЗ", url=c.objects_url,
                retrieved_at=(c.objects_evidence.retrieved_at if c.objects_evidence else _now()),
                title=pos.name, supplier_name=c.supplier_name, supplier_inn=c.supplier_inn,
                price=pos.unit_price, price_unit=pos.unit or "шт.", price_includes_vat=None, vat_rate=pos.vat,
                content=content, delivery_included=True, price_date=c.signed_at, contract_number=c.reestr,
                contract_status=c.status, customer=None, okpd2=pos.okpd2, ktru=pos.ktru,
                quantity_in_source=pos.quantity,
                characteristics=[OfferChar(name=a, value=v, unit=u, quote=f"{a}: {raw}", quote_verified=True)
                                 for a, v, u, raw in norm],
                text=text, evidence=c.objects_evidence,
                extra={"contract_url": c.url, "method": c.method, "prefilter_score": round(score, 2),
                       "position_index": pos.index},
            ))
        status.found += len(offers)
        await self.log("search", f"ЕИС: отобрано позиций контрактов для сверки — {len(offers)}", item=item.id,
                       source="eis44")
        return offers

    # ------------------------------------------------------------------ веб

    async def collect_web(self, item: Item, web: WebSource, status: SourceStatus) -> list[Offer]:
        cfg = web.cfg
        hits: list[WebHit] = []
        seen_urls: set[str] = set()
        for q in (item.web_queries or [f"{item.short_name or item.name} цена"])[: int(cfg.get("max_queries_per_item", 3))]:
            found, err = await web.search(q)
            status.requests += 1
            if err:
                status.errors += 1
                await self.log("search", f"Веб-поиск: «{q}» не выполнен: {err}", item=item.id, source="web")
                continue
            used = 0
            for h in found:
                ok, why = web.usable(h)
                if not ok:
                    await self.log("search", f"Пропущен {h.domain}: {why}", item=item.id, source="web", url=h.url)
                    continue
                if h.url in seen_urls:
                    continue
                seen_urls.add(h.url)
                hits.append(h)
                used += 1
            await self.log("search", f"Веб-поиск: «{q}» — результатов {len(found)}, к загрузке {used}", item=item.id,
                           source="web")
        hits = hits[: int(cfg.get("max_pages_per_item", 12))]
        pages = await asyncio.gather(*(web.fetch(h) for h in hits))
        keywords = list({*(item.short_name or item.name).split()[:3], *(item.brand or "").split(),
                         *(item.model or "").split(), *[r.name.split()[0] for r in item.requirements if r.mandatory]})
        offers: list[Offer] = []
        n = 0
        for h, page in zip(hits, pages):
            status.requests += 1
            if not page.ok:
                status.errors += 1
                await self.log("fetch", f"{h.domain}: страница не загружена — {page.error}", item=item.id, url=h.url)
                continue
            n += 1
            offers.append(Offer(
                id=f"{item.id}-w{n}", item_id=item.id, source=SourceKind.WEB, source_name=page.domain or h.domain,
                url=page.url, retrieved_at=page.evidence.retrieved_at if page.evidence else _now(),
                title=page.structured_name or page.title or h.title, supplier_name=page.seller_name or page.domain,
                supplier_inn=page.seller_inn, price=page.structured_price or Decimal("0"), price_unit="шт.",
                price_includes_vat=True, price_date=page.evidence.retrieved_at if page.evidence else _now(),
                text=fragment_for_llm(page, keywords), evidence=page.evidence,
                characteristics=[OfferChar(name=a, value=v, unit=u, quote=f"{a}: {raw}", quote_verified=True)
                                 for a, raw in page.specs for v, u in [split_value_unit(a, raw)]],
                extra={"query": h.query, "search_title": h.title, "page_prices": [str(p) for p, _ in page.prices[:8]],
                       **_text_price(page),
                       "price_from": "разметка schema.org" if page.structured_price else None,
                       "supplier_by": "ИНН" if page.seller_inn else ("наименование" if page.seller_name else "домен")},
            ))
        status.found += len(offers)
        await self.log("search", f"Сайты поставщиков: загружено страниц {len(offers)} из {len(hits)}", item=item.id,
                       source="web")
        return offers

    # ------------------------------------------------------------------ извлечение характеристик

    async def extract(self, item: Item, offers: list[Offer]) -> dict[str, dict[str, str]]:
        verdicts: dict[str, dict[str, str]] = {}
        if not offers:
            return verdicts
        if self.llm.enabled:
            batches = [offers[i:i + 6] for i in range(0, len(offers), 6)]
            results = await asyncio.gather(*(self._extract_safe(item, b) for b in batches))
            for res in results:
                verdicts.update(res)
            done = {oid for oid in verdicts}
            rest = [o for o in offers if o.id not in done]
            if rest:
                why = self.llm.last_error or "нет ответа"
                await self.log("llm", f"Модель не разобрала предложений: {len(rest)} ({why}); они сверены по правилам",
                               item=item.id, offers=[o.id for o in rest])
        else:
            rest = offers
            if self.llm.last_error:
                await self.log("llm", f"Модель недоступна ({self.llm.last_error}): предложения сверены по правилам",
                               item=item.id)
        for o in rest:
            self._extract_rules(item, o)
        return verdicts

    async def _extract_safe(self, item: Item, batch: list[Offer]) -> dict[str, dict[str, str]]:
        """Пакет предложений в модель; если ответа нет, пакет делится пополам и отправляется ещё раз."""
        res = await self._extract_batch(item, batch)
        if res or len(batch) == 1:
            return res
        half = len(batch) // 2
        parts = await asyncio.gather(self._extract_batch(item, batch[:half]), self._extract_batch(item, batch[half:]))
        return {**parts[0], **parts[1]}

    async def _extract_batch(self, item: Item, batch: list[Offer]) -> dict[str, dict[str, str]]:
        payload = []
        for o in batch:
            # тексты публичные (страницы магазинов, реестр ЕИС); телефоны, e-mail и ИНН уже вырезаны при сборе
            payload.append({"id": o.id, "source": o.source_name, "title": o.title[:300],
                            "known_price": str(o.price) if o.price and o.price > 0 else None,
                            "price_unit_in_source": o.price_unit if o.source != SourceKind.WEB else None,
                            "text": o.text[:3500]})
        data = await self.llm.tool_json(system=OFFERS_SYSTEM, user=offers_user(item, payload), tool_name="save_offers",
                                        tool_description="Сохранить разбор предложений", schema=OFFERS_SCHEMA,
                                        purpose=f"сверка предложений: {item.short_name or item.name}"[:120],
                                        max_tokens=9000)
        out: dict[str, dict[str, str]] = {}
        if not data:
            return out
        by_id = {o.id: o for o in batch}
        req_ids = {r.id for r in item.requirements}
        for x in data.get("offers", []):
            o = by_id.get(str(x.get("id")))
            if o is None:
                continue
            try:
                out[o.id] = self._apply_model_offer(item, o, x, req_ids)
            except (ArithmeticError, ValueError, TypeError, KeyError, AttributeError) as exc:
                o.extra["note"] = f"ответ модели по предложению не разобран ({type(exc).__name__}), сверка по правилам"
        return out

    def _apply_model_offer(self, item: Item, o: Offer, x: dict[str, Any], req_ids: set[str]) -> dict[str, str]:
        """Ответ модели по одному предложению: цена, фасовка, характеристики с проверкой цитат, вердикты."""
        o.extra["relevant"] = bool(x.get("relevant"))
        o.extra["note"] = (x.get("note") or "")[:300]
        o.extra["product_name"] = (x.get("product_name") or "")[:300]
        if o.source == SourceKind.WEB:
            p = _num(x.get("price"))
            if p and (o.price is None or o.price <= 0):
                quote = x.get("price_quote") or ""
                pd = p.quantize(Decimal("0.01"))
                digits = re.sub(r"\D", "", str(int(pd)))
                if digits and digits in re.sub(r"\D", "", o.text):
                    o.price = pd
                    o.extra["price_from"] = f"текст страницы: «{quote[:80]}»"
            if x.get("price_unit"):
                o.price_unit = str(x["price_unit"])[:60]
            if x.get("price_includes_vat") is not None:
                o.price_includes_vat = bool(x["price_includes_vat"])
            if x.get("delivery_included") is not None:
                o.delivery_included = bool(x["delivery_included"])
            if _num(x.get("delivery_cost")):
                o.delivery_cost = _num(x["delivery_cost"])
        if _num(x.get("content_value")) and x.get("content_unit") and o.content is None:
            cq = x.get("content_quote") or ""
            if not cq or quote_found(cq, o.text) or quote_found(cq, o.title):
                o.content = Quantity(value=_num(x["content_value"]), unit=str(x["content_unit"]))
                o.extra["content_quote"] = cq[:150]
        chars = []
        for ch in x.get("characteristics") or []:
            rid = ch.get("requirement_id")
            if rid not in req_ids:
                continue
            q = ch.get("quote") or ""
            chars.append(OfferChar(requirement_id=rid, name=next(r.name for r in item.requirements if r.id == rid),
                                   value=str(ch.get("value") or ""), unit=ch.get("unit"), quote=q[:200],
                                   quote_verified=quote_found(q, o.text) or quote_found(q, o.title)))
        if chars:   # исходные характеристики источника сохраняются, модель только сопоставляет их с ТЗ
            o.characteristics = [c for c in o.characteristics if not c.requirement_id] + chars
        return {v["requirement_id"]: v["status"] for v in (x.get("verdicts") or [])
                if v.get("requirement_id") in req_ids}

    def _extract_rules(self, item: Item, o: Offer) -> None:
        """Без модели: фасовка из названия; характеристики сопоставляет сверка (по смыслу названия и единицам)."""
        if o.content is None and item.content is not None:
            prefer = canon_unit(item.content.unit)
            q = parse_quantity(o.title, prefer=prefer) or parse_quantity(o.text[:400], prefer=prefer)
            if q is not None and dimension(canon_unit(q.unit)) == dimension(prefer):
                o.content = q
        if o.price is None or o.price <= 0:
            tp = o.extra.get("text_price")
            if tp:
                o.price = Decimal(tp)
                o.extra["price_from"] = f"текст страницы рядом с названием товара: «{o.extra.get('text_price_ctx', '')}»"
        if "relevant" not in o.extra:
            sim = name_similarity(item.short_name or item.name, f"{o.title} {o.text[:600]}", self.rules.matching.token_synonyms)
            o.extra["relevant"] = sim >= 0.5
            o.extra["note"] = (f"сверка без модели: совпадает {round(sim * 100)} % слов названия позиции"
                               if sim >= 0.5 else "без модели: в названии и описании мало слов из названия позиции")

    # ------------------------------------------------------------------ правовые проверки

    async def check_legal(self, item: Item, result: ItemResult, eis: EisSource) -> bool:
        """Неустойки по контрактам ЕИС и РНП по ИНН поставщиков — для цен, прошедших сверку и приведение (п. 3.13,
        ч. 18 ст. 22 44-ФЗ). Возвращает True, если найдено основание исключить цену (нужен пересчёт)."""
        skip = {DecisionCode.UNSUITABLE, DecisionCode.NOT_CONFIRMED, DecisionCode.NO_PRICE, DecisionCode.NO_UNIT,
                DecisionCode.TOO_OLD, DecisionCode.NOT_EXECUTED}
        codes = {d.offer_id: d.code for d in result.decisions}
        cand = [o for o in result.offers if o.source != SourceKind.MANUAL and codes.get(o.id) not in skip]
        if not cand:
            return False
        found = False
        n_pen = n_rnp = 0
        for o in cand:
            if o.source == SourceKind.EIS44 and o.contract_number:
                o.penalties, o.extra["penalties_url"] = await eis.penalties(o.contract_number)
                n_pen += 1
                found = found or bool(o.penalties)
            if o.supplier_inn:
                o.extra["rnp"], o.extra["rnp_url"] = await eis.in_rnp(o.supplier_inn)
                n_rnp += 1
                found = found or bool(o.extra["rnp"])
            o.extra["legal_checked"] = True
        if not n_pen and not n_rnp:      # у отобранных цен нет ни контрактов ЕИС, ни ИНН продавца
            return False
        pen = sum(1 for o in cand if o.penalties)
        rnp = sum(1 for o in cand if o.extra.get("rnp"))
        unknown = sum(1 for o in cand if (o.source == SourceKind.EIS44 and o.contract_number and o.penalties is None)
                      or (o.supplier_inn and o.extra.get("rnp") is None))
        await self.log("legal", f"Позиция {item.number}: проверены неустойки по контрактам ({n_pen}) и РНП по ИНН "
                                f"поставщиков ({n_rnp}); неустойки — {pen}, в РНП — {rnp}"
                                + (f", не удалось проверить — {unknown}" if unknown else ""), item=item.id)
        return found

    # ------------------------------------------------------------------ позиция

    async def process_item(self, item: Item, eis: EisSource, web: Optional[WebSource],
                           st_eis: SourceStatus, st_web: SourceStatus, delivery_required: Optional[bool],
                           overrides: Optional[dict[str, tuple[bool, str]]] = None) -> ItemResult:
        await self.log("item", f"Позиция {item.number}: {item.name}", item=item.id)
        try:
            return await self._process_item(item, eis, web, st_eis, st_web, delivery_required, overrides)
        except Exception as exc:  # сбой одной позиции не останавливает расчёт остальных
            msg = f"Сбой обработки позиции: {type(exc).__name__}. Цена по позиции не сформирована, повторите расчёт."
            await self.log("error", f"Позиция {item.number}: {msg}", item=item.id, detail=str(exc)[:300])
            return ItemResult(item=item, calc=CalcResult(status=CalcStatus.INSUFFICIENT, message=msg))

    async def _process_item(self, item: Item, eis: EisSource, web: Optional[WebSource],
                            st_eis: SourceStatus, st_web: SourceStatus, delivery_required: Optional[bool],
                            overrides: Optional[dict[str, tuple[bool, str]]] = None) -> ItemResult:
        tasks = [self.collect_eis(item, eis, st_eis)]
        if web is not None:
            tasks.append(self.collect_web(item, web, st_web))
        found = await asyncio.gather(*tasks)
        offers = [o for group in found for o in group]
        verdicts = await self.extract(item, offers)
        for o in offers:
            o.extra["verdicts"] = verdicts.get(o.id, {})
        result = evaluate_item(item, offers, self.rules, self.calc_date, self.cpi, delivery_required, overrides)
        if await self.check_legal(item, result, eis):
            result = evaluate_item(item, offers, self.rules, self.calc_date, self.cpi, delivery_required, overrides)
        calc = result.calc
        if calc is not None:
            await self.log("calc", f"Позиция {item.number}: {calc.message}", item=item.id, status=calc.status.value)
        return result

    # ------------------------------------------------------------------ прогон

    async def run(self, doc: Document) -> Report:
        placeholder = Procurement(title=doc.name, items=[], source_name=doc.name, source_sha256=doc.sha256)
        self.report = Report(id=self.id, created_at=_now(), procurement=placeholder)
        self.report.settings.update({
            "llm": self.llm.enabled, "llm_model": self.llm.model if self.llm.enabled else None,
            "web": self.use_web, "calc_date": self.calc_date.isoformat(),
            "rules": self.rules.model_dump(mode="json"),
            "cpi": "загружен" if self.cpi is not None else "нет данных",
            "legal_checks": True,
        })
        await self.log("start", "Анализ начат", llm=self.llm.enabled, web=self.use_web)
        http = HttpClient(self.settings)
        try:
            proc = await self.build_card(doc)
            self.report.procurement = proc
            eis = EisSource(http, self.settings.sources.get("eis44", {}))
            web = WebSource(http, self.settings, self.settings.sources.get("web", {})) if self.use_web else None
            st_eis = SourceStatus(source="ЕИС, реестр контрактов 44-ФЗ", state="ok")
            st_web = SourceStatus(source="Сайты поставщиков (Яндекс Search API)",
                                  state="ok" if web is not None else "disabled",
                                  message="" if web is not None else "не задан ключ поискового API или поиск выключен")
            results = await asyncio.gather(*(self.process_item(it, eis, web, st_eis, st_web, proc.delivery_included)
                                             for it in proc.items))
            self.report.items = list(results)
            for st in (st_eis, st_web):
                if st.state == "ok" and st.errors and st.errors >= max(1, st.requests // 2):
                    st.state = "partial"
            st_eis.message = st_eis.message or f"запросов {st_eis.requests}, ошибок {st_eis.errors}"
            if web is not None:
                blocked = ", ".join(sorted(http.blocked_hosts)) or "нет"
                st_web.message = f"запросов {st_web.requests}, ошибок {st_web.errors}; отклонили запрос сайты: {blocked}"
            self.report.sources = [st_eis, st_web]
            self.finish()
            await self.log("done", self.summary_line())
        except Exception as exc:  # отчёт сохраняется и при сбое
            self.report.status = "failed"
            self.report.error = f"{type(exc).__name__}: {exc}"
            await self.log("error", f"Сбой анализа: {self.report.error}")
        finally:
            await http.aclose()
            await self.llm.aclose()
            self.report.llm = {**self.llm.usage, "outbound": self.llm.outbound[-50:],
                               "error_log": self.llm.error_log[-20:]}
            self.report.finished_at = _now()
        return self.report

    def finish(self) -> None:
        total = Decimal("0")
        all_final = bool(self.report.items)
        for r in self.report.items:
            if r.calc and r.calc.status == CalcStatus.FINAL and r.calc.total is not None:
                total += r.calc.total
            else:
                all_final = False
        self.report.total = total if total > 0 else None
        self.report.all_final = all_final
        self.report.status = "done"

    def summary_line(self) -> str:
        n_final = sum(1 for r in self.report.items if r.calc and r.calc.status == CalcStatus.FINAL)
        total = f"{self.report.total:,.2f}".replace(",", " ").replace(".", ",") if self.report.total else "—"
        return f"Готово: позиций {len(self.report.items)}, с обоснованной ценой {n_final}; НМЦД по ним {total} руб."



def _text_price(page: Any) -> dict[str, str]:
    """Цена из текста страницы без разметки: первая цена после названия товара (не дальше 1 500 знаков).
    Используется только без модели; источник цены помечается в отчёте."""
    if page.structured_price is not None or not page.prices or not page.title:
        return {}
    text = page.text
    head = " ".join(page.title.split()[:3])
    start = text.lower().find(head.lower()) if len(head) >= 6 else -1
    if start < 0:
        return {}
    for value, ctx in page.prices:
        pos = text.find(ctx)
        if 0 <= start <= pos <= start + 1500:
            return {"text_price": str(value), "text_price_ctx": " ".join(ctx.split())[:120]}
    return {}


def evaluate_item(item: Item, offers: list[Offer], rules: Rules, calc_date: date, cpi: Optional[CpiSeries],
                  delivery_required: Optional[bool],
                  overrides: Optional[dict[str, tuple[bool, str]]] = None) -> ItemResult:
    """Сверка, приведение, отбор и расчёт по уже найденным предложениям."""
    matches: list[MatchResult] = []
    for o in offers:
        if o.source == SourceKind.MANUAL:
            matches.append(MatchResult(offer_id=o.id, cls=MatchClass.IDENTICAL, score=1.0,
                                       reasons=["коммерческое предложение на товар по ТЗ, внесено специалистом"]))
            continue
        if o.extra.get("relevant") is False:
            matches.append(MatchResult(offer_id=o.id, cls=MatchClass.UNSUITABLE, score=0.0,
                                       reasons=[f"другой товар: {o.extra.get('note') or 'не совпадает вид товара'}"],
                                       llm_used=bool(o.extra.get("verdicts"))))
            continue
        matches.append(match_offer(item, o, rules, o.extra.get("verdicts") or None))
    norms = [normalize_offer(item, o, rules, calc_date, cpi, delivery_required) for o in offers]
    decisions, calc = decide_and_calculate(item, offers, matches, norms, rules, calc_date, overrides)
    return ItemResult(item=item, offers=offers, matches=matches, normalizations=norms, decisions=decisions,
                      calc=calc, queries=[{"eis": item.eis_queries, "web": item.web_queries}])


def add_manual_offer(report: Report, item_id: str, supplier: str, inn: Optional[str], price: Decimal,
                     price_day: date, no_vat: bool, note: str) -> Offer:
    """Цена из коммерческого предложения (п. 3.7.1): за единицу закупки, с доставкой, как в запросе КП."""
    res = next(r for r in report.items if r.item.id == item_id)
    item = res.item
    n = sum(1 for o in res.offers if o.source == SourceKind.MANUAL) + 1
    offer = Offer(
        id=f"{item_id}-kp{n}", item_id=item_id, source=SourceKind.MANUAL, source_name="коммерческое предложение",
        url="", retrieved_at=_now(), title=item.short_name or item.name, supplier_name=supplier,
        supplier_inn=inn or None, price=price, price_unit=item.unit,
        price_includes_vat=None if no_vat else True, vat_rate="без НДС" if no_vat else None,
        content=item.content, delivery_included=True,
        price_date=datetime(price_day.year, price_day.month, price_day.day, 12, tzinfo=timezone.utc),
        text=f"Коммерческое предложение. {note}".strip(),
        extra={"manual": True, "note": note, "relevant": True, "kp_number": n},
    )
    res.offers.append(offer)
    return offer


async def rnp_check(settings: Settings, inn: str) -> tuple[Optional[bool], str]:
    """Проверка ИНН по реестру недобросовестных поставщиков вне прогона (для цены из коммерческого предложения)."""
    http = HttpClient(settings)
    try:
        return await EisSource(http, settings.sources.get("eis44", {})).in_rnp(inn)
    finally:
        await http.aclose()


def recalculate(report: Report, overrides_by_item: dict[str, dict[str, tuple[bool, str]]]) -> Report:
    """Пересчёт после решений специалиста без повторного поиска (на тех же предложениях)."""
    rules = load_rules()
    cpi = CpiSeries.load()
    calc_date = date.fromisoformat(report.settings.get("calc_date") or date.today().isoformat())
    items = [evaluate_item(r.item, r.offers, rules, calc_date, cpi, report.procurement.delivery_included,
                           overrides_by_item.get(r.item.id)) for r in report.items]
    total = Decimal("0")
    all_final = bool(items)
    for r in items:
        if r.calc and r.calc.status == CalcStatus.FINAL and r.calc.total is not None:
            total += r.calc.total
        else:
            all_final = False
    report.items = items
    report.total = total if total > 0 else None
    report.all_final = all_final
    report.settings["overrides"] = {k: {oid: [v[0], v[1]] for oid, v in d.items()} for k, d in overrides_by_item.items()}
    report.trace.append(TraceEvent(ts=_now(), stage="override",
                                   message="Пересчёт по решениям специалиста",
                                   data={"overrides": report.settings["overrides"]}))
    return report
