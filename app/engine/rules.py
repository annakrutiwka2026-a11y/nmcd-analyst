"""Правила расчёта и сопоставления: модель параметров и загрузка из config/rules.yaml.

Все числа, от которых зависит решение, лежат здесь. Логика ядра читает их из объекта Rules
и не содержит собственных порогов.
"""
from __future__ import annotations

import hashlib
import json
import warnings
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = ROOT / "config" / "rules.yaml"

# В расчётах число точное (Decimal), в JSON-выгрузке правил — обычное число.
Num = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]


def _num(text: str) -> Decimal:
    return Decimal(text)


class MethodRules(BaseModel):
    """Формула, достаточность данных и сроки."""

    model_config = ConfigDict(extra="ignore")

    formula: Literal["mean", "median", "min"] = "mean"
    min_suppliers: int = Field(default=3, ge=1)
    cv_threshold: Num = Field(default=_num("0.33"), gt=0)
    max_age_months: int = Field(default=36, ge=1)
    cpi_threshold_months: int = Field(default=6, ge=0)
    selection: Literal["stepwise", "strict"] = "stepwise"
    # chain — цепное произведение месячных индексов; sum — сумма месячных приростов, как записано в п. 3.18.
    cpi_formula: Literal["chain", "sum"] = "chain"
    cpi_from_price_month: bool = False
    cpi_max_lag_months: int = Field(default=3, ge=1)
    executed_statuses: list[str] = Field(default_factory=lambda: ["исполнение завершено", "исполнен", "исполнено"])


class OutlierRules(BaseModel):
    """Правило выбросов: модифицированный z-score и отношение к медиане."""

    model_config = ConfigDict(extra="ignore")

    mad_min_n: int = Field(default=5, ge=3)
    mad_z: Num = Field(default=_num("3.5"), gt=0)
    ratio_flag: Num = Field(default=_num("1.5"), gt=1)
    ratio_exclude: Num = Field(default=_num("2.0"), gt=1)
    # цена дальше чем в ratio_exclude раз от медианы исключается при любом числе цен, не только при малом
    ratio_any_n: bool = True

    @model_validator(mode="after")
    def _order(self) -> "OutlierRules":
        if self.ratio_exclude < self.ratio_flag:
            raise ValueError("outliers.ratio_exclude не может быть меньше outliers.ratio_flag")
        return self


class MatchingRules(BaseModel):
    """Сверка характеристик и классы «идентичное / однородное / неподходящее»."""

    model_config = ConfigDict(extra="ignore")

    identical_delta: Num = Field(default=_num("0.20"), ge=0)
    min_score: Num = Field(default=_num("0.35"), ge=0, le=1)
    eq_tolerance: Num = Field(default=_num("0"), ge=0)
    name_similarity: Num = Field(default=_num("0.8"), gt=0, le=1)
    weight_mandatory: Num = Field(default=_num("2"), gt=0)
    weight_optional: Num = Field(default=_num("1"), ge=0)
    above_credit: Num = Field(default=_num("0.8"), ge=0, le=1)
    missing_optional_credit: Num = Field(default=_num("0.4"), ge=0, le=1)
    model_confirms: bool = True
    # одна неподтверждённая существенная характеристика при двух и более подтверждённых — однородное, а не исключение
    one_unconfirmed_homogeneous: bool = True
    use_title: bool = True
    unverified_quote: Literal["reject", "accept"] = "reject"
    pair_by_unit: list[str] = Field(default_factory=lambda: ["г/м²"])
    token_synonyms: dict[str, str] = Field(default_factory=lambda: {
        "пачк": "упаковк", "вес": "масс", "изготовител": "производител", "граммаж": "масс", "плотност": "масс",
    })


class NormalizeRules(BaseModel):
    """Приведение цены к единице закупки."""

    model_config = ConfigDict(extra="ignore")

    assume_equal_units: bool = True
    bulk_units: list[str] = Field(default_factory=lambda: ["коробка", "ящик", "блок", "паллета"])
    content_from_title: bool = True
    # фасовка по массе или объёму пересчитывается пропорционально, только если отличается от ТЗ
    # не больше чем во столько раз (цена реактивов от фасовки зависит нелинейно)
    max_content_ratio: Num = Field(default=_num("2"), ge=1)


class VatRules(BaseModel):
    """Ставки НДС и дата их смены."""

    model_config = ConfigDict(extra="ignore")

    rate_2026: Num = Field(default=_num("0.22"), ge=0, lt=1)
    rate_before_2026: Num = Field(default=_num("0.20"), ge=0, lt=1)
    change_date: date = date(2026, 1, 1)

    def rate_at(self, day: date) -> Decimal:
        """Основная ставка НДС, действовавшая в указанный день."""
        return self.rate_2026 if day >= self.change_date else self.rate_before_2026


class CardRules(BaseModel):
    """Карточка позиции при разборе ТЗ без модели: какие характеристики считаются существенными."""

    model_config = ConfigDict(extra="ignore")

    # существенные — характеристики, название которых начинается с этих слов (общий список для любых категорий)
    essential_names: list[str] = Field(default_factory=lambda: [
        "вид", "тип", "формат", "модель", "марка", "класс", "технология", "назначение", "материал", "состав",
        "плотность", "масса листа", "масса бумаги", "область печати", "рабочая область", "объём", "объем",
        "ёмкость", "емкость", "мощность", "производительность", "разрешение", "диагональ", "система экспрессии",
        "клеточная линия", "штамм", "действующее вещество", "концентрация"])
    max_essential: int = Field(default=4, ge=1)       # не больше стольких существенных на позицию
    descriptive_len: int = Field(default=80, ge=20)   # длинное описание не делается существенным


class Rules(BaseModel):
    """Все параметры правил. Умолчания совпадают с поставляемым config/rules.yaml."""

    model_config = ConfigDict(extra="allow")

    method: MethodRules = Field(default_factory=MethodRules)
    outliers: OutlierRules = Field(default_factory=OutlierRules)
    matching: MatchingRules = Field(default_factory=MatchingRules)
    normalize: NormalizeRules = Field(default_factory=NormalizeRules)
    vat: VatRules = Field(default_factory=VatRules)
    card: CardRules = Field(default_factory=CardRules)
    rounding: Num = Field(default=_num("0.01"), gt=0)

    def digest(self) -> str:
        """Короткий отпечаток набора правил: одинаковые правила дают одинаковую строку."""
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def unknown_keys(data: dict[str, Any]) -> list[str]:
    """Параметры известных разделов, которых нет в модели (обычно опечатка в названии)."""
    found: list[str] = []
    for section, model in (("method", MethodRules), ("outliers", OutlierRules), ("matching", MatchingRules),
                           ("normalize", NormalizeRules), ("vat", VatRules), ("card", CardRules)):
        block = data.get(section)
        if isinstance(block, dict):
            found += [f"{section}.{key}" for key in block if key not in model.model_fields]
    return sorted(found)


def load_rules(path: str | None = None) -> Rules:
    """Читает правила из YAML. Без пути берётся config/rules.yaml; если его нет — умолчания."""
    target = Path(path) if path else DEFAULT_RULES_PATH
    if not target.is_file():
        if path:
            raise FileNotFoundError(f"Файл правил не найден: {target}")
        return Rules()
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Файл правил {target} должен содержать разделы «ключ: значение»")
    extra = unknown_keys(data)
    if extra:
        warnings.warn(f"{target.name}: неизвестные параметры пропущены: {', '.join(extra)}", stacklevel=2)
    return Rules.model_validate(data)
