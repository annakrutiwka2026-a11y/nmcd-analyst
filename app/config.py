"""Настройки сервиса: переменные окружения, пути и файлы конфигурации."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CERTS_DIR = ROOT / "data" / "certs"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Читает .env без сторонних библиотек; уже заданные переменные окружения важнее."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Settings(BaseModel):
    data_dir: Path
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    llm_model: str = "claude-sonnet-5"
    llm_max_tokens_per_run: int = 600_000
    yandex_api_key: str | None = None
    yandex_folder_id: str | None = None
    contact_url: str = ""
    org_name: str = "Заказчик"
    disable_llm: bool = False
    disable_web: bool = False
    public: bool = False                 # общедоступный стенд: на главной только свои расчёты посетителя
    max_queue: int = 6                   # сколько расчётов может ждать и идти одновременно
    llm_daily_budget: int = 0            # предел токенов модели в сутки на весь сервис (0 — без предела)
    sources: dict[str, Any] = {}

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key) and not self.disable_llm

    @property
    def web_enabled(self) -> bool:
        return bool(self.yandex_api_key and self.yandex_folder_id) and not self.disable_web

    @property
    def user_agent(self) -> str:
        about = f"+{self.contact_url}" if self.contact_url else "price research for public procurement"
        return f"NMCD-Analyst/1.0 ({about})"


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "да"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = {**_read_dotenv(ROOT / ".env"), **os.environ}
    sources_path = CONFIG_DIR / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.is_file() else {}
    data_dir = Path(env.get("NMCD_DATA_DIR") or (ROOT / "var"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        anthropic_api_key=env.get("LLM_API_KEY") or None,
        anthropic_base_url=env.get("LLM_BASE_URL") or "https://api.anthropic.com",
        llm_model=env.get("LLM_MODEL") or "claude-sonnet-5",
        llm_max_tokens_per_run=int(env.get("LLM_MAX_TOKENS_PER_RUN") or 600_000),
        yandex_api_key=env.get("YANDEX_SEARCH_API_KEY") or None,
        yandex_folder_id=env.get("YANDEX_FOLDER_ID") or None,
        contact_url=env.get("NMCD_CONTACT_URL") or "",
        org_name=env.get("NMCD_ORG_NAME") or "Заказчик",
        disable_llm=_flag(env.get("NMCD_DISABLE_LLM")),
        disable_web=_flag(env.get("NMCD_DISABLE_WEB")),
        public=_flag(env.get("NMCD_PUBLIC")),
        max_queue=int(env.get("NMCD_MAX_QUEUE") or 6),
        llm_daily_budget=int(env.get("LLM_DAILY_TOKEN_BUDGET") or 0),
        sources=sources or {},
    )
