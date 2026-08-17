"""Composite market-data + PNG rendering tool for native Telegram visuals."""

import asyncio
from typing import Any

from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.services import yfinance_client as yfc
from atlas.services.util import now_iso
from atlas.visuals.market import candlestick_png, correlation_png, performance_png

KINDS = {"candlestick", "performance", "correlation"}
PERIODS = {"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"}
_RENDER_LOCK = asyncio.Lock()


@tool(
    "render_market_image",
    """Fetch sourced OHLCV history and attach a polished PNG directly in Telegram. Use
    candlestick for one symbol, performance to compare normalized returns, and correlation for
    a daily-return heatmap. This tool fetches its own Yahoo Finance data; do not call another
    history tool first.""",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(KINDS)},
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One symbol for candlestick; 2-8 for comparisons.",
            },
            "period": {"type": "string", "enum": sorted(PERIODS)},
        },
        "required": ["kind", "symbols"],
    },
)
async def render_market_image(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind") or "").lower()
    period = str(args.get("period") or "3mo").lower()
    symbols = list(dict.fromkeys(
        str(symbol).upper().strip() for symbol in (args.get("symbols") or []) if str(symbol).strip()
    ))[:8]

    if kind not in KINDS:
        return {"error": "unsupported market image kind"}
    if period not in PERIODS:
        return {"error": "unsupported chart period"}
    if kind == "candlestick" and len(symbols) != 1:
        return {"error": "candlestick charts require exactly one symbol"}
    if kind != "candlestick" and len(symbols) < 2:
        return {"error": "comparison images require at least two symbols"}

    histories = await asyncio.gather(*(yfc.history_frame(symbol, period) for symbol in symbols))
    frames = {
        symbol: history for symbol, history in zip(symbols, histories)
        if not isinstance(history, dict) or "error" not in history
    }
    missing = [symbol for symbol in symbols if symbol not in frames]
    if kind == "candlestick" and missing:
        return {"error": f"no OHLCV history available for {symbols[0]}"}
    if kind != "candlestick" and len(frames) < 2:
        return {"error": "fewer than two symbols returned overlapping history"}

    try:
        async with _RENDER_LOCK:  # pyplot/mplfinance mutate process-global state
            if kind == "candlestick":
                image = await asyncio.to_thread(candlestick_png, frames[symbols[0]], symbols[0], period)
                title = f"{symbols[0]} candlestick · {period}"
            elif kind == "performance":
                image = await asyncio.to_thread(performance_png, frames, period)
                title = f"{' vs '.join(frames)} · {period} performance"
            else:
                image = await asyncio.to_thread(correlation_png, frames, period)
                title = f"{' · '.join(frames)} correlation · {period}"
    except Exception as exc:
        return {"error": f"chart rendering failed: {type(exc).__name__}: {str(exc)[:120]}"}

    attachments = ctx.extra.setdefault("telegram_photos", [])
    attachments.append(
        {
            "bytes": image,
            "filename": f"atlas_{kind}_{'_'.join(frames)}.png",
            "caption": f"{title}\nSource: Yahoo Finance · {now_iso()}",
        }
    )
    return ok(
        {"attached_to_telegram": True, "kind": kind, "symbols": list(frames),
         "period": period, "missing_symbols": missing},
        source="Yahoo Finance",
    )
