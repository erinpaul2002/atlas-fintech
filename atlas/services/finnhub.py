"""Finnhub — news, earnings calendar, metrics, and a second quote source.

Recon (6 Aug 2026): /quote, /company-news, /calendar/earnings and /stock/metric all 200 on the
free tier. Documented 60 calls/min, never observed.
"""

from datetime import date, timedelta
from typing import Any

from atlas.config import settings
from atlas.services.util import fetch_json, now_iso

BASE = "https://finnhub.io/api/v1"


async def _get(path: str, params: dict[str, Any], what: str) -> Any:
    return await fetch_json(f"{BASE}{path}", params={**params, "token": settings.finnhub_api_key}, what=what)


async def quote(symbol: str) -> dict[str, Any]:
    """Fall-through partner for yfinance. c=current, pc=previous close, d/dp=change."""
    symbol = symbol.upper().strip()
    raw = await _get("/quote", {"symbol": symbol}, f"finnhub quote {symbol}")
    if "error" in raw:
        return raw
    if not raw.get("c"):
        return {"error": f"no Finnhub quote for {symbol}"}
    return {
        "symbol": symbol,
        "price": raw["c"],
        "previous_close": raw.get("pc"),
        "change": raw.get("d"),
        "change_pct": raw.get("dp"),
        "day_high": raw.get("h"),
        "day_low": raw.get("l"),
        "as_of": now_iso(),
    }


async def company_news(symbol: str, days: int = 7, limit: int = 8) -> Any:
    symbol = symbol.upper().strip()
    today = date.today()
    raw = await _get(
        "/company-news",
        {"symbol": symbol, "from": str(today - timedelta(days=days)), "to": str(today)},
        f"finnhub news {symbol}",
    )
    if isinstance(raw, dict):
        return raw if "error" in raw else {"error": f"no news for {symbol}"}
    return [
        {
            "headline": n.get("headline", ""),
            "summary": (n.get("summary") or "")[:400],
            "source": n.get("source", ""),
            "url": n.get("url", ""),
            "datetime": n.get("datetime"),
        }
        for n in raw[:limit]
    ]


async def earnings_calendar(symbol: str | None = None, days_ahead: int = 14, days_back: int = 45) -> Any:
    today = date.today()
    params: dict[str, Any] = {
        "from": str(today - timedelta(days=days_back)),
        "to": str(today + timedelta(days=days_ahead)),
    }
    if symbol:
        params["symbol"] = symbol.upper().strip()
    raw = await _get("/calendar/earnings", params, "finnhub earnings calendar")
    if "error" in raw:
        return raw
    return raw.get("earningsCalendar", [])


async def metrics(symbol: str) -> Any:
    symbol = symbol.upper().strip()
    raw = await _get("/stock/metric", {"symbol": symbol, "metric": "all"}, f"finnhub metrics {symbol}")
    if "error" in raw:
        return raw
    return raw.get("metric", {}) or {"error": f"no metrics for {symbol}"}
