"""Модели данных сервиса: закупка, позиции, предложения, решения, расчёт и отчёт.

Деньги и количества хранятся в Decimal. Все модели сериализуются в JSON без потерь,
чтобы отчёт можно было пересчитать независимо.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- требования


class Op(str, Enum):
    """Условие требования ТЗ."""

    EQ = "="          # равно (с допуском, если он указан)
    GE = ">="         # не менее
    LE = "<="         # не более
    GT = ">"          # более
    LT = "<"          # менее
    BETWEEN = "between"  # от value до value_max включительно
    IN = "in"         # одно из значений списка
    CONTAINS = "contains"  # текст должен присутствовать (марка, технология, состав)
    PRESENT = "present"    # наличие функции/свойства («наличие», «соответствие»)


class Quantity(BaseModel):
    """Количество с единицей измерения, например 10 мкг или 500 листов."""

    value: Decimal
    unit: str


class Requirement(BaseModel):
    """Одна характеристика из ТЗ."""

    id: str
    name: str
    op: Op
    value: Optional[str] = None          # число в виде строки или текст
    value_max: Optional[str] = None      # верхняя граница для BETWEEN
    values: list[str] = Field(default_factory=list)  # для IN
    unit: Optional[str] = None
    tolerance: Optional[str] = None      # допуск, например «±3%» или «±2»
    mandatory: bool = True               # существенная характеристика
    by_brand: bool = False               # значение однозначно задано товарным знаком и моделью из ТЗ
    quote: str = ""                      # дословный фрагмент ТЗ


class Item(BaseModel):
    """Позиция закупки (одна строка спецификации)."""

    id: str
    number: int
    name: str
    short_name: str = ""                 # краткое название для поиска
    brand: Optional[str] = None          # товарный знак, если указан в ТЗ
    model: Optional[str] = None          # модель, если указана
    okpd2: Optional[str] = None
    ktru: Optional[str] = None
    quantity: Decimal
    unit: str                            # единица закупки: шт., пачка, упаковка
    content: Optional[Quantity] = None   # содержимое одной единицы (500 листов, не менее 10 мкг)
    content_op: Op = Op.EQ               # условие на содержимое (= или >=)
    requirements: list[Requirement] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)  # фразы для поиска (рус/лат синонимы)
    eis_queries: list[str] = Field(default_factory=list)     # короткие фразы для реестра контрактов ЕИС
    web_queries: list[str] = Field(default_factory=list)     # запросы для поиска предложений поставщиков
    negative_terms: list[str] = Field(default_factory=list)  # что точно не подходит (б/у, набор ELISA и т. п.)


class Procurement(BaseModel):
    """Разобранное описание закупки."""

    title: str
    customer: Optional[str] = None
    delivery_place: Optional[str] = None
    delivery_terms: Optional[str] = None
    delivery_included: Optional[bool] = None  # требует ли ТЗ включить доставку в цену
    items: list[Item]
    source_name: str = ""                # имя файла или «текст»
    source_sha256: str = ""
    parse_mode: str = "rules"            # rules | llm
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- предложения


class SourceKind(str, Enum):
    EIS44 = "eis44"     # реестр контрактов ЕИС, 44-ФЗ
    EIS223 = "eis223"   # реестр договоров ЕИС, 223-ФЗ
    WEB = "web"         # сайт поставщика, найденный через поисковый API
    MANUAL = "manual"   # цена, добавленная пользователем (КП и т. п.)


class OfferChar(BaseModel):
    """Характеристика предложения, выписанная из источника."""

    requirement_id: Optional[str] = None  # с каким требованием ТЗ сопоставлена
    name: str
    value: str
    unit: Optional[str] = None
    quote: str = ""                       # дословная цитата из источника
    quote_verified: bool = False          # цитата найдена в тексте источника


class Evidence(BaseModel):
    """Сохранённая копия источника."""

    url: str
    retrieved_at: datetime
    snapshot_path: Optional[str] = None   # путь к сохранённой копии (gzip)
    sha256: Optional[str] = None
    http_status: Optional[int] = None


class Offer(BaseModel):
    """Найденное ценовое предложение (позиция контракта или карточка товара)."""

    id: str
    item_id: str
    source: SourceKind
    source_name: str                      # «ЕИС, реестр контрактов 44-ФЗ», «komus.ru»
    url: str
    retrieved_at: datetime
    title: str
    supplier_name: Optional[str] = None
    supplier_inn: Optional[str] = None
    price: Decimal                        # цена за единицу источника, руб.
    price_unit: str                       # единица, за которую указана цена
    price_includes_vat: Optional[bool] = None
    vat_rate: Optional[str] = None        # «22%», «20%», «Без НДС» и т. п.
    content: Optional[Quantity] = None    # содержимое единицы источника (25 мкг, 500 листов, 5 пачек)
    delivery_included: Optional[bool] = None
    delivery_cost: Optional[Decimal] = None   # стоимость доставки на заказ, если указана
    min_order_qty: Optional[Decimal] = None
    price_date: Optional[datetime] = None     # дата контракта или дата получения цены
    contract_number: Optional[str] = None     # реестровый номер ЕИС
    contract_status: Optional[str] = None     # «Исполнение завершено» и т. п.
    penalties: Optional[bool] = None          # были ли неустойки (если известно)
    customer: Optional[str] = None
    okpd2: Optional[str] = None
    ktru: Optional[str] = None
    quantity_in_source: Optional[Decimal] = None  # объём поставки в контракте
    characteristics: list[OfferChar] = Field(default_factory=list)
    text: str = ""                        # фрагмент текста источника для проверки цитат
    evidence: Optional[Evidence] = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- сверка и решения


class MatchClass(str, Enum):
    IDENTICAL = "identical"       # идентичное
    HOMOGENEOUS = "homogeneous"   # однородное
    UNSUITABLE = "unsuitable"     # неподходящее


class CheckStatus(str, Enum):
    OK = "ok"
    FAIL = "fail"
    MISSING = "missing"     # в источнике нет данных
    ABOVE = "above"         # требование выполнено с запасом больше допуска
    UNCLEAR = "unclear"


class CharCheck(BaseModel):
    requirement_id: str
    requirement: str          # человекочитаемое требование: «Масса листа: 80 ± 3 г/м²»
    offer_value: Optional[str] = None
    status: CheckStatus
    mandatory: bool = True
    note: str = ""


class MatchResult(BaseModel):
    offer_id: str
    cls: MatchClass
    score: float = 0.0              # релевантность 0..1
    checks: list[CharCheck] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)   # почему такой класс (по-русски)
    llm_used: bool = False
    not_confirmed: bool = False     # неподходящее потому, что существенная характеристика не подтверждена источником


class NormStep(BaseModel):
    kind: str                 # unit | content | vat | delivery | cpi | tier
    factor: Decimal
    note: str                 # объяснение по-русски


class Normalization(BaseModel):
    offer_id: str
    ok: bool                  # удалось ли привести к единице закупки
    unit_price: Optional[Decimal] = None   # приведённая цена за единицу закупки, руб.
    steps: list[NormStep] = Field(default_factory=list)
    problem: Optional[str] = None          # почему привести не удалось
    flags: list[str] = Field(default_factory=list)


class DecisionCode(str, Enum):
    INCLUDED = "INCLUDED"
    NO_PRICE = "NO_PRICE"
    NO_UNIT = "NO_UNIT"                 # единицу не удалось привести
    NO_DATE = "NO_DATE"
    TOO_OLD = "TOO_OLD"
    NOT_EXECUTED = "NOT_EXECUTED"
    PENALTIES = "PENALTIES"
    RNP = "RNP"
    ANONYMOUS = "ANONYMOUS"             # нет сведений о продавце
    UNSUITABLE = "UNSUITABLE"           # не выполнено обязательное требование
    NOT_CONFIRMED = "NOT_CONFIRMED"     # обязательное требование не подтверждено источником
    HOMOGENEOUS_EXCLUDED = "HOMOGENEOUS_EXCLUDED"  # однородное при достаточном числе идентичных
    DUPLICATE = "DUPLICATE"             # дубль (та же цена того же поставщика / та же страница)
    DUP_SUPPLIER = "DUP_SUPPLIER"       # повторная цена того же поставщика
    OUTLIER_MAD = "OUTLIER_MAD"
    OUTLIER_RATIO = "OUTLIER_RATIO"
    USER_EXCLUDED = "USER_EXCLUDED"
    USER_INCLUDED = "USER_INCLUDED"


class Decision(BaseModel):
    offer_id: str
    included: bool
    code: DecisionCode
    reason: str                         # понятное объяснение по-русски
    flags: list[str] = Field(default_factory=list)  # MAD_ONLY, SUSPICIOUS_RATIO, NON_VAT_PAYER, ...
    actor: str = "system"


class CalcStatus(str, Enum):
    FINAL = "final"                     # цена обоснована
    INSUFFICIENT = "insufficient"       # данных недостаточно, цена не формируется


class Hint(BaseModel):
    without_offer_id: str
    cv: Decimal
    mean: Decimal


class CalcResult(BaseModel):
    status: CalcStatus
    set_used: str = "A"                 # A — идентичные, B — идентичные и однородные
    n: int = 0
    n_suppliers: int = 0
    prices: list[Decimal] = Field(default_factory=list)
    offer_ids: list[str] = Field(default_factory=list)
    mean: Optional[Decimal] = None
    median: Optional[Decimal] = None
    min: Optional[Decimal] = None
    max: Optional[Decimal] = None
    sd: Optional[Decimal] = None
    cv: Optional[Decimal] = None        # в долях: 0.1234 = 12,34 %
    unit_price: Optional[Decimal] = None   # средняя цена за единицу (п. 3.21)
    total: Optional[Decimal] = None        # НМЦД позиции = количество × средняя цена
    message: str = ""                   # итоговое пояснение по-русски
    hints: list[Hint] = Field(default_factory=list)
    method: str = "Метод сопоставимых рыночных цен, формула п. 3.21 Методических рекомендаций (приказ Минэкономразвития России № 567)"


class ItemResult(BaseModel):
    item: Item
    offers: list[Offer] = Field(default_factory=list)
    matches: list[MatchResult] = Field(default_factory=list)
    normalizations: list[Normalization] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    calc: Optional[CalcResult] = None
    queries: list[dict[str, Any]] = Field(default_factory=list)  # что и где искали


# ---------------------------------------------------------------- отчёт


class SourceStatus(BaseModel):
    source: str
    state: str                 # ok | partial | error | disabled
    requests: int = 0
    errors: int = 0
    found: int = 0
    message: str = ""


class TraceEvent(BaseModel):
    ts: datetime
    stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class Report(BaseModel):
    id: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    procurement: Procurement
    items: list[ItemResult] = Field(default_factory=list)
    total: Optional[Decimal] = None    # сумма НМЦД по позициям с итоговым статусом
    status: str = "running"            # running | done | failed
    all_final: bool = False
    sources: list[SourceStatus] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)  # модель, число вызовов, токены
    error: Optional[str] = None
