"""Quotes and the market-wide snapshot. Composite: one call fans out, never one call per symbol."""

import asyncio
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.services import finnhub
from atlas.services import yfinance_client as yfc

ET = ZoneInfo("America/New_York")

INDICES = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq", "^DJI": "Dow", "^VIX": "VIX"}
SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLI": "Industrials", "SMH": "Semiconductors",
}


def session_state(now: datetime | None = None) -> str:
    """Which tape the price came off. Without this the model says "trading at" on a Sunday.

    ponytail: ignores exchange holidays — a half-day or a Thanksgiving reads as open.
    Wire a calendar only if that shows up in a real reply.
    """
    et = (now or datetime.now(ET)).astimezone(ET)
    if et.weekday() >= 5:
        return "weekend, market closed — last close"
    clock = et.time()
    if clock < time(4, 0) or clock >= time(20, 0):
        return "market closed — last close"
    if clock < time(9, 30):
        return "pre-market"
    if clock < time(16, 0):
        return "market open"
    return "after hours"


async def one_quote(symbol: str) -> dict[str, Any]:
    """yfinance first, Finnhub second. Both cover this field, so a failure on one is not a gap."""
    primary = await yfc.quote(symbol)
    if "error" not in primary:
        return {**primary, "source": "Yahoo Finance"}
    backup = await finnhub.quote(symbol)
    if "error" not in backup:
        return {**backup, "source": "Finnhub"}
    return {"symbol": symbol.upper(), "error": primary.get("error", "no quote available")}


async def quotes_for(symbols: list[str]) -> list[dict[str, Any]]:
    clean = [s.strip().upper() for s in symbols if s and s.strip()][:20]
    return list(await asyncio.gather(*(one_quote(s) for s in clean)))


@tool(
    "get_quote",
    """Live price, day change, day range, volume and market cap for one or more symbols.
    Batch every symbol you need into a single call. Use this for any price a reply mentions —
    prices are never recalled from memory.""",
    {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ticker symbols, e.g. ['NVDA', 'AMD']",
            }
        },
        "required": ["symbols"],
    },
)
async def get_quote(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbols = args.get("symbols") or []
    if not symbols:
        return {"error": "no symbols given"}
    rows = await quotes_for(symbols)
    if all("error" in r for r in rows):
        return {"error": "couldn't get a live quote for " + ", ".join(r["symbol"] for r in rows)}
    return ok({"quotes": rows, "session": session_state()}, source="Yahoo Finance / Finnhub")


@tool(
    "market_snapshot",
    """The state of the market right now: index levels, sector performance, how the user's own
    watchlist is doing, and which companies report earnings today. Use for "how's the market",
    "anything I should know today", or when building a view of the session.""",
    {
        "type": "object",
        "properties": {
            "include_sectors": {"type": "boolean", "description": "Include sector ETF performance. Default true."}
        },
    },
)
async def market_snapshot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    include_sectors = args.get("include_sectors", True)
    watch = [w.symbol for w in ctx.user.watchlist][:10]

    index_task = quotes_for(list(INDICES))
    sector_task = quotes_for(list(SECTOR_ETFS)) if include_sectors else _none()
    watch_task = quotes_for(watch) if watch else _none()
    earnings_task = finnhub.earnings_calendar(days_ahead=1, days_back=0)

    indices, sectors, watchlist, earnings = await asyncio.gather(
        index_task, sector_task, watch_task, earnings_task
    )

    data: dict[str, Any] = {
        "session": session_state(),
        "indices": [_label(q, INDICES) for q in indices if "error" not in q],
    }
    if sectors:
        ranked = sorted(
            (_label(q, SECTOR_ETFS) for q in sectors if "error" not in q),
            key=lambda r: r.get("change_pct") or 0,
            reverse=True,
        )
        data["sectors_best_to_worst"] = ranked
    if watchlist:
        data["their_watchlist"] = [
            {**q, "reason_they_watch_it": _reason(ctx, q.get("symbol", ""))} for q in watchlist if "error" not in q
        ]
    if isinstance(earnings, list):
        data["reporting_today"] = [
            {"symbol": e.get("symbol"), "when": e.get("hour"), "eps_estimate": e.get("epsEstimate")}
            for e in earnings[:12]
        ]
    return ok(data, source="Yahoo Finance, Finnhub")


async def _none() -> list[dict[str, Any]]:
    return []


def _label(quote: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        "name": names.get(quote.get("symbol", ""), quote.get("symbol")),
        "symbol": quote.get("symbol"),
        "price": quote.get("price"),
        "change_pct": quote.get("change_pct"),
    }


def _reason(ctx: ToolContext, symbol: str) -> str:
    for w in ctx.user.watchlist:
        if w.symbol == symbol:
            return w.reason
    return ""
