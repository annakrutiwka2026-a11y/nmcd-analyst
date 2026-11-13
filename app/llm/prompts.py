"""Промты и схемы ответов модели. Модель работает только с переданным текстом и отвечает строгим JSON."""
from __future__ import annotations

import json
from typing import Any

from app.models import Item, Procurement

_REQ_OPS = ["=", ">=", "<=", ">", "<", "between", "in", "contains", "present"]

CARD_SYSTEM = """Ты методолог контрактной службы. По техническому заданию (ТЗ) составь карточки позиций для поиска рыночных цен методом сопоставимых рыночных цен.

Правила:
1. Позиции и их номера, количество и единицу бери из таблицы спецификации (она дана отдельно). Не объединяй и не дроби позиции.
2. requirements — характеристики позиции из ТЗ. Для каждой: короткое name; op ("=", ">=", "<=", ">", "<", "between", "in", "contains", "present"); value, value_max (для between), values (для in), unit, tolerance («±3%» или «±3»), quote — дословный фрагмент ТЗ до 200 символов.
3. mandatory = true только у 2–4 характеристик, без которых это уже другой товар и другая цена: вид и тип товара, модель, формат и масса (плотность) листа бумаги, технология и область построения оборудования, вид белка и система экспрессии (клеточная линия), оригинальность расходного материала или запчасти (оригинальный или совместимый). Числовая характеристика, от которой прямо зависит цена (размер, масса, плотность, мощность, объём, производительность), — существенная. Показатели качества, которые поставщики обычно не публикуют и которые проверяются при приёмке по документам (эндотоксин, ED50, белизна, толщина, непрозрачность), сроки и условия хранения, документы, гарантия, упаковка, цвет, страна происхождения — mandatory = false.
4. Товарный знак из ТЗ запиши в brand, модель изделия производителя — в model («D600 PRO», «Classic»); если в ТЗ есть и артикул производителя, запиши через косую черту («59A / CF259A»). Обозначение вещества или белка (rhIL-2), марка бумаги по ГОСТ, артикул и ГОСТ — не модель: для них model = null. Не путай производителя-завод с товарным знаком.
4а. by_brand = true у характеристики, значение которой однозначно определено моделью из ТЗ: у любого экземпляра этой модели оно одно и то же (область печати у модели D600 PRO, ресурс у картриджа 59A). Без модели by_brand = false: один товарный знак характеристик не задаёт. Если в ТЗ названы и знак, и модель, by_brand = true у всех технических характеристик модели (тип, цвет, ресурс, размеры, мощность). Формат, фасовка, оригинальность (совместимый товар носит тот же номер модели), новизна и всё, что у того же знака бывает разным, — false. Если в ТЗ нет ни товарного знака, ни модели — false у всех характеристик.
5. content — содержимое одной единицы закупки, от которого зависит цена: число листов в пачке, масса белка в одной штуке («не менее 10 мкг» → content_value 10, content_unit «мкг», content_op ">="). Если такого нет — null. Эту характеристику не повторяй в requirements.
6. eis_queries — 2–3 короткие фразы, как такой товар называют в контрактах ЕИС: вид товара + знак/модель или ключевой признак, без лишних цифр («Бумага для офисной техники SvetoCopy», «Интерлейкин-2 рекомбинантный»).
7. web_queries — 2–3 запроса для поиска цен у интернет-магазинов и дистрибьюторов России: модель или вид товара, 1–2 ключевых параметра, слово «цена». Для реактивов добавь латинское обозначение и массу фасовки («rhIL-2 recombinant human 100 мкг цена»).
8. negative_terms — 2–5 слов, по которым видно, что предложение о другом товаре (например «набор ИФА», «антитела», «картридж», «б/у»).
9. Не придумывай значений, которых нет в ТЗ. Метки вида [ORG_1], [ADDR_1], [MONEY_1] — скрытые сведения, их не восстанавливай."""

CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "name": {"type": "string"},
                    "short_name": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "brand": {"type": ["string", "null"]},
                    "model": {"type": ["string", "null"]},
                    "content_value": {"type": ["number", "null"]},
                    "content_unit": {"type": ["string", "null"]},
                    "content_op": {"type": "string", "enum": ["=", ">="]},
                    "requirements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"type": "string", "enum": _REQ_OPS},
                                "value": {"type": ["string", "null"]},
                                "value_max": {"type": ["string", "null"]},
                                "values": {"type": "array", "items": {"type": "string"}},
                                "unit": {"type": ["string", "null"]},
                                "tolerance": {"type": ["string", "null"]},
                                "mandatory": {"type": "boolean"},
                                "by_brand": {"type": "boolean"},
                                "quote": {"type": "string"},
                            },
                            "required": ["name", "op", "value", "value_max", "values", "unit", "tolerance",
                                         "mandatory", "by_brand", "quote"],
                        },
                    },
                    "eis_queries": {"type": "array", "items": {"type": "string"}},
                    "web_queries": {"type": "array", "items": {"type": "string"}},
                    "negative_terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["number", "name", "short_name", "quantity", "unit", "brand", "model", "content_value", "content_unit",
                             "content_op", "requirements", "eis_queries", "web_queries", "negative_terms"],
            },
        },
    },
    "required": ["title", "items"],
}


def card_user(redacted_text: str, skeleton: Procurement) -> str:
    rows = "\n".join(f"{it.number}) {it.name} — {it.quantity} {it.unit}" for it in skeleton.items)
    return f"Таблица спецификации:\n{rows}\n\nТекст ТЗ:\n{redacted_text[:60000]}"


OFFERS_SYSTEM = """Ты сверяешь ценовые предложения с позицией закупки. Работай только с текстом предложений: что в тексте не сказано, то не подтверждено. Свои знания о товарах и товарных знаках не используй.

Для каждого предложения верни:
- relevant: true, если это тот же вид товара, что в позиции. Другой товарный знак — всё ещё relevant=true (однородный товар). Другая фасовка или упаковка (250 листов вместо 500, 50 мкг вместо 10 мкг, коробка из 5 пачек) — relevant=true: цену приведут к единице закупки отдельно. relevant=false — аксессуар, расходник, набор, другой белок или вещество, другой формат (А3 вместо А4), другая модификация модели («D600 Pro 2 HS» вместо «D600 Pro»), лекарственный препарат вместо реактива;
- product_name: название товара из текста;
- price: цена одной единицы продажи в рублях из текста (одной пачки, штуки, флакона, коробки; если цен несколько — та, что относится к этому товару, без скидок по карте и акций «от N штук»); price_unit: за что цена («пачка», «шт.», «флакон 50 мкг», «коробка 5 пачек»); price_quote: дословный фрагмент с ценой; price_includes_vat: true/false/null по тексту;
- content_value и content_unit: сколько содержит единица, за которую указана цена (500 листов, 25 мкг, 1 шт.), content_quote — дословно. Для реестра ЕИС фасовку бери из характеристик позиции («Количество листов в пачке: 500»);
- delivery_included / delivery_cost: только если в тексте прямо сказано;
- characteristics: значения для требований позиции (requirement_id из списка, value, unit, quote — дословный фрагмент текста предложения, не длиннее 150 символов). Только то, что есть в тексте. Характеристики реестра ЕИС записаны по каталогу КТРУ: «Масса бумаги площадью 1м2: 80 г/м²» — это плотность (масса листа), «≥ 146 %» — не менее 146 %;
- verdicts: для каждого требования позиции — "ok" (текст подтверждает), "fail" (текст противоречит), "missing" (в тексте нет), "unclear" (сказано неоднозначно). Если значения нет в тексте, ставь "missing", даже если товар известный;
- note: одна короткая фраза по-русски, почему предложение подходит или нет."""

OFFERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "product_name": {"type": "string"},
                    "price": {"type": ["number", "null"]},
                    "price_unit": {"type": ["string", "null"]},
                    "price_quote": {"type": ["string", "null"]},
                    "price_includes_vat": {"type": ["boolean", "null"]},
                    "content_value": {"type": ["number", "null"]},
                    "content_unit": {"type": ["string", "null"]},
                    "content_quote": {"type": ["string", "null"]},
                    "delivery_included": {"type": ["boolean", "null"]},
                    "delivery_cost": {"type": ["number", "null"]},
                    "characteristics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "requirement_id": {"type": "string"},
                                "value": {"type": "string"},
                                "unit": {"type": ["string", "null"]},
                                "quote": {"type": "string"},
                            },
                            "required": ["requirement_id", "value", "unit", "quote"],
                        },
                    },
                    "verdicts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "requirement_id": {"type": "string"},
                                "status": {"type": "string", "enum": ["ok", "fail", "missing", "unclear"]},
                            },
                            "required": ["requirement_id", "status"],
                        },
                    },
                    "note": {"type": "string"},
                },
                "required": ["id", "relevant", "product_name", "price", "price_unit", "price_quote",
                             "price_includes_vat", "content_value", "content_unit", "content_quote",
                             "delivery_included", "delivery_cost", "characteristics", "verdicts", "note"],
            },
        }
    },
    "required": ["offers"],
}


def item_brief(item: Item) -> str:
    reqs = []
    for r in item.requirements:
        cond = r.op.value
        val = r.value or ""
        if r.op.value == "between":
            val = f"{r.value}…{r.value_max}"
        elif r.op.value == "in":
            val = ", ".join(r.values)
        mark = "существенная" if r.mandatory else "второстепенная"
        tol = f" {r.tolerance}" if r.tolerance else ""
        reqs.append(f"- {r.id}: {r.name} {cond} {val}{tol} {r.unit or ''} ({mark})".replace("  ", " "))
    content = ""
    if item.content is not None:
        content = f"\nСодержимое единицы закупки: {item.content_op.value} {item.content.value} {item.content.unit}"
    brand = f"\nТоварный знак: {item.brand}" if item.brand else ""
    model = f"\nМодель: {item.model}" if item.model else ""
    return (f"Позиция: {item.name}\nЕдиница закупки: {item.unit}{content}{brand}{model}\n"
            f"Требования:\n" + ("\n".join(reqs) if reqs else "- нет"))


def offers_user(item: Item, batch: list[dict[str, str]]) -> str:
    return item_brief(item) + "\n\nПредложения (JSON):\n" + json.dumps(batch, ensure_ascii=False)
