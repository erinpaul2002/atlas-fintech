"""yfinance — quotes, fundamentals, history. No key. Blocking, so everything goes to a thread.

Recon (6 Aug 2026): first call after a cold start cost 2253ms, then ~250ms steady — hence the
warm-up at startup. Thin client, no business logic: assembly lives in agent/tools/market.py.
"""

import logging
from typing import Any

import yfinance as yf

from atlas.services.util import TTLCache, in_thread, now_iso

log = logging.getLogger(__name__)

_quotes = TTLCache(ttl=20)
_info = TTLCache(ttl=900)


def _fast(symbol: str) -> dict[str, Any]:
    t = yf.Ticker(symbol)
    fi = t.fast_info
    price = fi.get("lastPrice")
    # `previousClose` is not the prior session's close — observed 220.74 for NVDA when the last
    # two closes were 211.94 and 219.22. `regularMarketPreviousClose` is the one that matches
    # the tape (and Finnhub). Getting the day change wrong is the most visible error available.
    prev = fi.get("regularMarketPreviousClose") or fi.get("previousClose")
    if price is None:
        raise ValueError(f"no price for {symbol}")
    change = (price - prev) if prev else None
    return {
        "symbol": symbol.upper(),
        "price": round(float(price), 4),
        "previous_close": round(float(prev), 4) if prev else None,
        "change": round(float(change), 4) if change is not None else None,
        "change_pct": round(float(change) / float(prev) * 100, 2) if change is not None and prev else None,
        "day_high": fi.get("dayHigh"),
        "day_low": fi.get("dayLow"),
        "year_high": fi.get("yearHigh"),
        "year_low": fi.get("yearLow"),
        "volume": fi.get("lastVolume"),
        "market_cap": fi.get("marketCap"),
        "currency": fi.get("currency") or "USD",
        "as_of": now_iso(),
    }


async def quote(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    cached = _quotes.get(symbol)
    if cached:
        return cached
    out = await in_thread(_fast, symbol, timeout=12, what=f"yfinance quote {symbol}")
    return _quotes.put(symbol, out) if "error" not in out else out


def _raw_info(symbol: str) -> dict[str, Any]:
    return dict(yf.Ticker(symbol).info or {})


async def info(symbol: str) -> dict[str, Any]:
    """`.info` — the wide dict: profile, margins, multiples, analyst targets. ~1-2s uncached."""
    symbol = symbol.upper().strip()
    cached = _info.get(symbol)
    if cached:
        return cached
    out = await in_thread(_raw_info, symbol, timeout=20, what=f"yfinance info {symbol}")
    if "error" in out or not out.get("longName"):
        return out if "error" in out else {"error": f"no profile data for {symbol}"}
    return _info.put(symbol, out)


def _history(symbol: str, period: str) -> list[dict[str, Any]]:
    df = yf.Ticker(symbol).history(period=period)
    if df is None or df.empty:
        raise ValueError(f"no history for {symbol}")
    return [
        {
            "date": str(idx.date()),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        }
        for idx, row in df.iterrows()
    ]


async def history(symbol: str, period: str = "1mo") -> Any:
    return await in_thread(_history, symbol.upper().strip(), period, timeout=20, what=f"yfinance history {symbol}")


def _income(symbol: str) -> dict[str, Any]:
    df = yf.Ticker(symbol).income_stmt
    if df is None or df.empty:
        raise ValueError(f"no income statement for {symbol}")
    cols = list(df.columns)[:2]
    return {
        str(col.date()): {
            str(k): (float(v) if v == v else None)
            for k, v in df[col].head(12).to_dict().items()
        }
        for col in cols
    }


async def income_statement(symbol: str) -> Any:
    return await in_thread(_income, symbol.upper().strip(), timeout=25, what=f"yfinance income {symbol}")


async def warmup() -> None:
    """Pay the 2.2s cold start before a user does."""
    await quote("AAPL")
