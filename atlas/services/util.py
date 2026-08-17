"""Shared plumbing for provider clients: timeout, one retry, an error dict instead of a raise.

A raised exception here becomes a stack trace in someone's chat. Every failure leaves as
`{"error": "..."}` that the model can read out loud.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    return _http


async def close_http() -> None:
    if _http is not None and not _http.is_closed:
        await _http.aclose()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def fetch_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    what: str = "request",
) -> Any:
    for attempt in range(2):
        try:
            r = await http().get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt:
                log.warning("%s failed: %s", what, str(exc)[:200])
                return {"error": f"{what} unavailable ({type(exc).__name__})"}
            await asyncio.sleep(0.4)
    return {"error": f"{what} unavailable"}


async def in_thread(fn: Callable[..., Any], *args, timeout: float = 12.0, what: str = "call") -> Any:
    """Blocking library (yfinance, pandas) → thread, with a deadline and one retry."""
    for attempt in range(2):
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
        except Exception as exc:
            if attempt:
                log.warning("%s failed: %s", what, str(exc)[:200])
                return {"error": f"{what} unavailable ({type(exc).__name__})"}
            await asyncio.sleep(0.3)
    return {"error": f"{what} unavailable"}


def failed(value: Any) -> bool:
    return not isinstance(value, (list, float, int)) and (not value or "error" in value)


class TTLCache:
    """Quotes go stale in seconds and company profiles in hours — one knob, two lifetimes."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        return None

    def put(self, key: str, value: Any) -> Any:
        self._data[key] = (time.monotonic(), value)
        return value
