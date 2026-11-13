"""HTTP-клиент для источников: robots.txt, паузы между запросами, повторы, кеш и сохранённые копии страниц.

Защиту сайтов не обходим: если сайт отдаёт капчу или страницу-заглушку антибота,
запрос считается неудачным, а домен попадает в стоп-лист на время работы сервиса.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import certifi
import httpx

from app.config import CERTS_DIR, Settings
from app.models import Evidence

_ANTIBOT = re.compile(
    r"captcha|ddos-guard|checking your browser|cf-browser-verification|__qrator|servicepipe|"
    r"проверка браузера|вы не робот|access denied|доступ ограничен",
    re.I,
)
_META_CHARSET = re.compile(rb"<meta[^>]+charset=['\"]?([a-zA-Z0-9_\-]+)", re.I)


def build_ssl_context() -> ssl.SSLContext:
    """Стандартные корневые сертификаты плюс корневой и выпускающий сертификаты Минцифры (для сайтов госорганов)."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    for name in ("russian_trusted_root_ca.pem", "russian_trusted_sub_ca.pem"):
        path = CERTS_DIR / name
        if path.is_file():
            ctx.load_verify_locations(cafile=str(path))
    return ctx


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def domain_matches(host: str, domains: list[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _decode(content: bytes, content_type: str) -> str:
    charset = None
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    else:
        mm = _META_CHARSET.search(content[:4096])
        if mm:
            charset = mm.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8", "cp1251"):
        if not enc:
            continue
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


class FetchResult:
    def __init__(self, url: str, status: int, text: str, content_type: str, retrieved_at: datetime,
                 from_cache: bool = False, evidence: Optional[Evidence] = None, error: Optional[str] = None,
                 final_url: Optional[str] = None):
        self.url = url
        self.final_url = final_url or url
        self.status = status
        self.text = text
        self.content_type = content_type
        self.retrieved_at = retrieved_at
        self.from_cache = from_cache
        self.evidence = evidence
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


class HttpClient:
    """Общий асинхронный клиент. Один экземпляр на прогон."""

    def __init__(self, settings: Settings):
        self.settings = settings
        cfg = settings.sources.get("http", {})
        self.timeout = float(cfg.get("timeout_sec", 25))
        self.retries = int(cfg.get("retries", 2))
        self.cache_ttl = float(cfg.get("cache_ttl_hours", 24)) * 3600
        self.max_bytes = int(cfg.get("max_page_bytes", 3_000_000))
        self.cache_dir = settings.data_dir / "cache" / "http"
        self.evidence_dir = settings.data_dir / "evidence"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            verify=build_ssl_context(),
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "ru,en;q=0.8"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._robots_lock = asyncio.Lock()
        self.blocked_hosts: dict[str, str] = {}
        self.stats: dict[str, dict[str, int]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ служебное

    def _stat(self, host: str, key: str) -> None:
        self.stats.setdefault(host, {"requests": 0, "errors": 0, "cache": 0})
        self.stats[host][key] = self.stats[host].get(key, 0) + 1

    async def _pace(self, host: str, min_interval: float) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, 0.0) + min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json.gz")

    def _cache_get(self, key: str) -> Optional[dict[str, Any]]:
        path = self._cache_path(key)
        if not path.is_file() or time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            return json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            return None

    def _cache_put(self, key: str, payload: dict[str, Any]) -> None:
        try:
            self._cache_path(key).write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode()))
        except OSError:
            pass

    def save_evidence(self, url: str, content: bytes, status: int, retrieved_at: datetime) -> Evidence:
        digest = hashlib.sha256(content).hexdigest()
        path = self.evidence_dir / f"{digest}.html.gz"
        if not path.exists():
            path.write_bytes(gzip.compress(content))
        return Evidence(url=url, retrieved_at=retrieved_at, snapshot_path=f"evidence/{digest}.html.gz",
                        sha256=digest, http_status=status)

    async def allowed(self, url: str) -> tuple[bool, str]:
        """Проверка robots.txt. 404 — разрешено, 401/403/5xx и сетевые ошибки — запрещено."""
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_lock:
            if base not in self._robots:
                rp: Optional[RobotFileParser] = RobotFileParser()
                try:
                    r = await self._client.get(base + "/robots.txt")
                    if r.status_code == 404:
                        rp.parse([])
                    elif r.status_code >= 400:
                        rp = None
                    else:
                        rp.parse(_decode(r.content, r.headers.get("content-type", "")).splitlines())
                except httpx.HTTPError:
                    rp = None
                self._robots[base] = rp
        rp = self._robots[base]
        if rp is None:
            return False, "robots.txt недоступен"
        if not rp.can_fetch(self.settings.user_agent, url):
            return False, "запрещено robots.txt"
        return True, ""

    # ------------------------------------------------------------ запросы

    async def get(self, url: str, params: Optional[dict[str, Any]] = None, *, min_interval: float = 1.0,
                  check_robots: bool = True, save_evidence: bool = True, use_cache: bool = True) -> FetchResult:
        req = httpx.Request("GET", url, params=params)
        full_url = str(req.url)
        host = host_of(full_url)
        now = datetime.now(timezone.utc)
        if host in self.blocked_hosts:
            return FetchResult(full_url, 0, "", "", now, error=self.blocked_hosts[host])
        cache_key = "GET " + full_url
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached:
                self._stat(host, "cache")
                ev = Evidence(**cached["evidence"]) if cached.get("evidence") else None
                return FetchResult(full_url, cached["status"], cached["text"], cached.get("content_type", ""),
                                   datetime.fromisoformat(cached["retrieved_at"]), True, ev,
                                   final_url=cached.get("final_url"))
        if check_robots:
            ok, why = await self.allowed(full_url)
            if not ok:
                return FetchResult(full_url, 0, "", "", now, error=why)
        last_error = "нет ответа"
        for attempt in range(self.retries + 1):
            await self._pace(host, min_interval)
            self._stat(host, "requests")
            try:
                r = await self._client.get(full_url)
            except httpx.HTTPError as exc:
                self._stat(host, "errors")
                last_error = f"сетевая ошибка: {type(exc).__name__}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            content = r.content[: self.max_bytes]
            text = _decode(content, r.headers.get("content-type", ""))
            retrieved_at = datetime.now(timezone.utc)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                self._stat(host, "errors")
                last_error = f"HTTP {r.status_code}"
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code in (401, 403) or (len(text) < 20000 and _ANTIBOT.search(text)):
                reason = "сайт отклонил автоматический запрос (антибот-защита), обход не выполняется"
                self.blocked_hosts[host] = reason
                self._stat(host, "errors")
                return FetchResult(full_url, r.status_code, "", r.headers.get("content-type", ""), retrieved_at,
                                   error=reason)
            if r.status_code >= 400:
                self._stat(host, "errors")
                return FetchResult(full_url, r.status_code, "", r.headers.get("content-type", ""), retrieved_at,
                                   error=f"HTTP {r.status_code}")
            evidence = self.save_evidence(full_url, content, r.status_code, retrieved_at) if save_evidence else None
            result = FetchResult(full_url, r.status_code, text, r.headers.get("content-type", ""), retrieved_at,
                                 evidence=evidence, final_url=str(r.url))
            if use_cache:
                self._cache_put(cache_key, {
                    "status": r.status_code, "text": text, "content_type": result.content_type,
                    "retrieved_at": retrieved_at.isoformat(), "final_url": str(r.url),
                    "evidence": evidence.model_dump(mode="json") if evidence else None,
                })
            return result
        return FetchResult(full_url, 0, "", "", datetime.now(timezone.utc), error=last_error)

    async def post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str], *,
                        min_interval: float = 0.2, use_cache: bool = True) -> tuple[Optional[dict[str, Any]], str]:
        """POST к API (поисковый API). Возвращает (json, ошибка)."""
        key = "POST " + url + " " + json.dumps(payload, sort_keys=True, ensure_ascii=False)
        host = host_of(url)
        if use_cache:
            cached = self._cache_get(key)
            if cached:
                self._stat(host, "cache")
                return cached.get("json"), ""
        last_error = ""
        for attempt in range(self.retries + 1):
            await self._pace(host, min_interval)
            self._stat(host, "requests")
            try:
                r = await self._client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                self._stat(host, "errors")
                last_error = f"сетевая ошибка: {type(exc).__name__}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                self._stat(host, "errors")
                last_error = f"HTTP {r.status_code}"
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                self._stat(host, "errors")
                return None, f"HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
            if use_cache:
                self._cache_put(key, {"json": data})
            return data, ""
        return None, last_error or "нет ответа"
