"""Клиент языковой модели: строгий JSON через вызов инструмента, кеш ответов и учёт токенов.

Модель подключается по Anthropic-совместимому API (адрес и ключ — в переменных окружения).
Если ключа нет, сервис работает по правилам без модели и пишет об этом в отчёте.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import random
from datetime import date
from pathlib import Path
from typing import Any, Optional

import anthropic

from app.config import Settings


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.llm_enabled
        self.model = settings.llm_model
        self.budget = settings.llm_max_tokens_per_run
        self.cache_dir: Path = settings.data_dir / "cache" / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage: dict[str, Any] = {"model": self.model, "calls": 0, "cached": 0, "input_tokens": 0,
                                      "output_tokens": 0, "errors": 0, "skipped_budget": 0}
        self.outbound: list[dict[str, Any]] = []   # что отправлялось во внешнюю модель
        self.error_log: list[dict[str, Any]] = []  # отказы и сбои модели (тип, код, начало сообщения)
        self.last_error: Optional[str] = None
        self._sem = asyncio.Semaphore(3)
        self._client: Optional[anthropic.AsyncAnthropic] = None
        if self.enabled:
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
                max_retries=0,          # повторы — в _create, с журналом
                timeout=240.0,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _day_file(self) -> Path:
        return self.settings.data_dir / "llm_usage" / f"{date.today().isoformat()}.txt"

    def _day_used(self) -> int:
        """Токены модели, израсходованные сервисом за текущие сутки (все расчёты вместе)."""
        try:
            return int(self._day_file().read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def _day_add(self, tokens: int) -> None:
        if not self.settings.llm_daily_budget or tokens <= 0:
            return
        path = self._day_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(self._day_used() + tokens), encoding="utf-8")
        except OSError:
            pass

    def _key(self, *parts: str) -> str:
        return hashlib.sha256("\x1f".join((self.model,) + parts).encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[dict[str, Any]]:
        path = self.cache_dir / f"{key}.json.gz"
        if path.is_file():
            try:
                return json.loads(gzip.decompress(path.read_bytes()))
            except (OSError, ValueError):
                return None
        return None

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        try:
            (self.cache_dir / f"{key}.json.gz").write_bytes(gzip.compress(json.dumps(value, ensure_ascii=False).encode()))
        except OSError:
            pass

    async def tool_json(self, *, system: str, user: str, tool_name: str, tool_description: str,
                        schema: dict[str, Any], purpose: str, max_tokens: int = 6000) -> Optional[dict[str, Any]]:
        """Один вызов модели с обязательным ответом в виде аргументов инструмента (строгий JSON)."""
        if not self.enabled or self._client is None:
            return None
        key = self._key(system, user, tool_name, json.dumps(schema, sort_keys=True, ensure_ascii=False))
        cached = self._cache_get(key)
        if cached is not None:
            self.usage["cached"] += 1
            self.outbound.append({"purpose": purpose, "chars": len(user), "cached": True,
                                  "sha256": hashlib.sha256(user.encode()).hexdigest()})
            return cached
        if self.usage["input_tokens"] + self.usage["output_tokens"] > self.budget:
            self.usage["skipped_budget"] += 1
            self.last_error = "исчерпан предел токенов на расчёт"
            return None
        if self.settings.llm_daily_budget and self._day_used() >= self.settings.llm_daily_budget:
            self.usage["skipped_budget"] += 1
            self.last_error = "исчерпан суточный предел токенов модели на сервис"
            return None
        tool = {"name": tool_name, "description": tool_description, "input_schema": schema}
        async with self._sem:
            for attempt, choice in enumerate(({"type": "tool", "name": tool_name}, {"type": "auto"})):
                sys_text = system if attempt == 0 else system + f"\n\nОтветь только вызовом инструмента {tool_name}."
                resp, exc = await self._create(model=self.model, max_tokens=max_tokens, system=sys_text,
                                               messages=[{"role": "user", "content": user}], tools=[tool],
                                               tool_choice=choice, thinking={"type": "disabled"})
                if resp is None:
                    if attempt == 0 and isinstance(exc, anthropic.BadRequestError) and "tool_choice" in str(exc):
                        continue
                    self._fail(purpose, exc)
                    return None
                self.usage["calls"] += 1
                spent = int(getattr(resp.usage, "input_tokens", 0) or 0), int(getattr(resp.usage, "output_tokens", 0) or 0)
                self.usage["input_tokens"] += spent[0]
                self.usage["output_tokens"] += spent[1]
                self._day_add(sum(spent))
                self.outbound.append({"purpose": purpose, "chars": len(user), "cached": False,
                                      "sha256": hashlib.sha256(user.encode()).hexdigest()})
                for block in resp.content:
                    if block.type == "tool_use" and block.name == tool_name and isinstance(block.input, dict):
                        self._cache_put(key, block.input)
                        return block.input
                if resp.stop_reason == "max_tokens":
                    self._fail(purpose, None, "ответ не уместился в лимит токенов")
                    return None
        self._fail(purpose, None, "модель не вызвала инструмент")
        return None

    async def _create(self, **kwargs: Any) -> tuple[Any, Optional[Exception]]:
        """Запрос к модели с повторами при перегрузке, лимите частоты и сетевых сбоях (паузы 3, 8, 20 с)."""
        last: Optional[Exception] = None
        for delay in (3, 8, 20, None):
            try:
                return await self._client.messages.create(**kwargs), None
            except anthropic.BadRequestError as exc:
                return None, exc
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                last = exc
            except anthropic.APIStatusError as exc:
                last = exc
                if exc.status_code not in (408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529):
                    return None, exc
            if delay is None:
                break
            await asyncio.sleep(delay * (1 + random.random() * 0.3))
        return None, last

    def _fail(self, purpose: str, exc: Optional[Exception], text: str = "") -> None:
        self.usage["errors"] += 1
        if text:
            msg = text
        elif exc is not None:
            status = getattr(exc, "status_code", None)
            msg = {401: "ключ модели не принят (401)", 402: "закончился баланс ключа модели (402)",
                   403: "доступ к модели запрещён (403)"}.get(status, type(exc).__name__ + (f" {status}" if status else ""))
            if status in (401, 402, 403):
                self.enabled = False          # до конца расчёта модель не вызывается: ответ не изменится
        else:
            msg = "нет ответа"
        detail = str(getattr(exc, "message", None) or exc)[:200] if exc is not None else ""
        self.last_error = msg
        self.error_log.append({"purpose": purpose[:120], "error": msg, "detail": detail})
