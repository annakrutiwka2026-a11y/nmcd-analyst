"""Шлюз обезличивания, сумма прописью, проверка цитат."""
from decimal import Decimal

from app.llm.redact import redact
from app.textutil import quote_found
from app.report.fmt import money, rubles_in_words

CANARY = "КАНАРЕЙКА-7f3a9"


def test_redaction_removes_customer_and_contacts():
    text = ("Заказчик: Федеральное государственное автономное образовательное учреждение высшего образования "
            "\"Московский физико-технический институт (национальный исследовательский университет)\". "
            "Адрес: 141701, Московская обл., г. Долгопрудный, Институтский пер., д. 9. Контакт: Иванов И.И., "
            "тел. +7 (495) 408-45-54, e-mail: zakupki@phystech.edu. ИНН 5008006211. НМЦД 1 250 000,00 руб. "
            f"Срок 12.11.2026. Метка {CANARY}. Бумага А4, плотность 80 г/м2, 500 листов, белизна 146 %.")
    red = redact(text, extra_terms=[CANARY])
    for secret in ("Московский физико-технический", "Долгопрудный", "Иванов", "408-45-54", "phystech.edu",
                   "5008006211", "1 250 000", "12.11.2026", CANARY):
        assert secret not in red.text, secret
    for kept in ("Бумага А4", "80 г/м2", "500 листов", "146 %"):
        assert kept in red.text, kept
    assert red.total >= 7 and not red.leaks


def test_rubles_in_words():
    assert rubles_in_words(Decimal("967200.00")) == "Девятьсот шестьдесят семь тысяч двести рублей 00 копеек"
    assert rubles_in_words(Decimal("1101.21")) == "Одна тысяча сто один рубль 21 копейка"
    assert rubles_in_words(Decimal("2000000.05")) == "Два миллиона рублей 05 копеек"


def test_money():
    assert money(Decimal("80600")) == "80 600,00 ₽"
    assert money(Decimal("1234567.891")) == "1 234 567,89 ₽"


def test_quote_found():
    text = "Бумага для офисной техники. Формат: А4; Плотность бумаги, г/м2: 80; Количество листов в пачке: 500Штука"
    assert quote_found("Плотность бумаги, г/м2: 80", text)
    assert not quote_found("Плотность бумаги 120", text)


def test_model_out_of_balance_stops_calls(tmp_path):
    """Ответ 402 (закончился баланс ключа): понятная причина в журнале, до конца расчёта модель не вызывается."""
    import asyncio
    import anthropic
    import httpx
    from app.config import Settings
    from app.llm.client import LLMClient
    llm = LLMClient(Settings(data_dir=tmp_path, anthropic_api_key="k", anthropic_base_url="https://example.invalid"))
    calls = []

    async def fake_create(**kwargs):
        calls.append(1)
        resp = httpx.Response(402, request=httpx.Request("POST", "https://example.invalid/v1/messages"))
        return None, anthropic.APIStatusError("Баланс ключа закончился", response=resp, body=None)

    llm._create = fake_create
    args = dict(system="s", tool_name="t", tool_description="d", schema={"type": "object"}, purpose="проверка")
    assert asyncio.run(llm.tool_json(user="1", **args)) is None
    assert llm.last_error == "закончился баланс ключа модели (402)" and llm.enabled is False
    assert asyncio.run(llm.tool_json(user="2", **args)) is None and len(calls) == 1
