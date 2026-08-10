"""The thick research tools. Each fans out server-side so a six-fetch question costs one
model round-trip and comes back complete — ARCHITECTURE.md §4."""

import asyncio
from typing import Any

from atlas.agent.tools.market import one_quote, session_state
from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.services import edgar, finnhub
from atlas.services import yfinance_client as yfc

SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Energy": "XLE", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Industrials": "XLI", "Consumer Defensive": "XLP",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
    "Communication Services": "XLC",
}

ANGLES = ("overview", "financials", "valuation", "news", "earnings", "filings", "full")
DIMENSIONS = ("growth", "profitability", "valuation", "momentum", "news")


def _num(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


def _pct(value: Any) -> Any:
    """yfinance returns ratios (0.62), Finnhub returns percentages (62.0). Normalise to percent."""
    return round(value * 100, 2) if isinstance(value, (int, float)) else None


def profile_of(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "employees": info.get("fullTimeEmployees"),
        "description": (info.get("longBusinessSummary") or "")[:700],
    }


def growth_of(info: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    m = metrics if isinstance(metrics, dict) and "error" not in metrics else {}
    return {
        "revenue_ttm": info.get("totalRevenue"),
        "revenue_growth_yoy_pct": _pct(info.get("revenueGrowth")) or _num(m.get("revenueGrowthTTMYoy")),
        "earnings_growth_yoy_pct": _pct(info.get("earningsGrowth")) or _num(m.get("epsGrowthTTMYoy")),
        "revenue_growth_3y_pct": _num(m.get("revenueGrowth3Y")),
    }


def profitability_of(info: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    m = metrics if isinstance(metrics, dict) and "error" not in metrics else {}
    return {
        "gross_margin_pct": _pct(info.get("grossMargins")) or _num(m.get("grossMarginTTM")),
        "operating_margin_pct": _pct(info.get("operatingMargins")) or _num(m.get("operatingMarginTTM")),
        "net_margin_pct": _pct(info.get("profitMargins")) or _num(m.get("netProfitMarginTTM")),
        "roe_pct": _pct(info.get("returnOnEquity")) or _num(m.get("roeTTM")),
        "free_cash_flow": info.get("freeCashflow"),
    }


def valuation_of(info: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    m = metrics if isinstance(metrics, dict) and "error" not in metrics else {}
    return {
        "market_cap": info.get("marketCap"),
        "trailing_pe": _num(info.get("trailingPE")) or _num(m.get("peTTM")),
        "forward_pe": _num(info.get("forwardPE")),
        "price_to_sales": _num(info.get("priceToSalesTrailing12Months")) or _num(m.get("psTTM")),
        "price_to_book": _num(info.get("priceToBook")) or _num(m.get("pbAnnual")),
        "peg_ratio": _num(info.get("trailingPegRatio")),
        "analyst_target_mean": info.get("targetMeanPrice"),
        "analyst_rating": info.get("recommendationKey"),
        "analyst_count": info.get("numberOfAnalystOpinions"),
    }


def momentum_of(quote: dict[str, Any], hist: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "price": quote.get("price"),
        "change_pct_1d": quote.get("change_pct"),
        "year_high": quote.get("year_high"),
        "year_low": quote.get("year_low"),
    }
    if isinstance(hist, list) and len(hist) > 1:
        closes = [h["close"] for h in hist]
        out["change_pct_5d"] = _pct((closes[-1] / closes[-6] - 1)) if len(closes) > 6 else None
        out["change_pct_1mo"] = _pct((closes[-1] / closes[0] - 1))
    return out


async def _bundle(symbol: str, need: set[str]) -> dict[str, Any]:
    """One symbol's raw feeds, only the ones a dimension actually needs."""
    jobs = {
        "quote": one_quote(symbol),
        "info": yfc.info(symbol) if need & {"growth", "profitability", "valuation", "overview"} else _skip(),
        "metrics": finnhub.metrics(symbol) if need & {"growth", "profitability", "valuation"} else _skip(),
        "hist": yfc.history(symbol, "1mo") if "momentum" in need else _skip(),
        "news": finnhub.company_news(symbol, days=5, limit=4) if "news" in need else _skip(),
    }
    done = await asyncio.gather(*jobs.values())
    return dict(zip(jobs.keys(), done))


async def _skip() -> dict[str, Any]:
    return {}


@tool(
    "research_company",
    """Everything about one company, sliced by angle: overview (what they do, sector, size),
    financials (revenue, growth, margins, cash flow), valuation (multiples, analyst targets),
    news (last few days), earnings (last and next report), filings (recent SEC filings), or
    full (all of it in one go). Use once you know which angle they want.""",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker symbol"},
            "angle": {"type": "string", "enum": list(ANGLES), "description": "Which slice to fetch"},
        },
        "required": ["symbol", "angle"],
    },
)
async def research_company(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbol = str(args.get("symbol", "")).upper().strip()
    angle = str(args.get("angle", "overview")).lower()
    if not symbol:
        return {"error": "no symbol given"}
    if angle not in ANGLES:
        angle = "full"

    wants = set(ANGLES[:-1]) if angle == "full" else {angle}
    # overview always: `.info` is where the company's name comes from, and it's cached 15 min
    need = {"overview"}
    if wants & {"financials", "valuation"}:
        need |= {"growth", "profitability", "valuation"}

    raw = await _bundle(symbol, need)
    news_task = finnhub.company_news(symbol, days=7, limit=6) if wants & {"news"} else _skip()
    earnings_task = finnhub.earnings_calendar(symbol) if wants & {"earnings"} else _skip()
    filings_task = edgar.recent_filings(symbol, limit=6) if wants & {"filings"} else _skip()
    news, earnings, recent_filings = await asyncio.gather(news_task, earnings_task, filings_task)

    info = raw["info"] if isinstance(raw["info"], dict) and "error" not in raw["info"] else {}
    data: dict[str, Any] = {"symbol": symbol, "session": session_state()}
    errors: list[str] = []

    if "overview" in wants:
        data["profile"] = profile_of(info) if info else {}
        data["quote"] = raw["quote"]
        if not info:
            errors.append("company profile unavailable")
    if wants & {"financials"}:
        data["growth"] = growth_of(info, raw["metrics"])
        data["profitability"] = profitability_of(info, raw["metrics"])
    if "valuation" in wants:
        data["valuation"] = valuation_of(info, raw["metrics"])
    if "news" in wants:
        data["news"] = news if isinstance(news, list) else []
        if not isinstance(news, list):
            errors.append("news unavailable")
    if "earnings" in wants:
        data["earnings"] = earnings[:4] if isinstance(earnings, list) else []
        if not isinstance(earnings, list):
            errors.append("earnings calendar unavailable")
    if "filings" in wants:
        data["filings"] = recent_filings if isinstance(recent_filings, list) else []
        if not isinstance(recent_filings, list):
            errors.append("SEC filings unavailable")

    if errors:
        data["errors"] = errors  # half an answer beats no answer
    return ok(data, source="Yahoo Finance, Finnhub, SEC EDGAR")


@tool(
    "compare_companies",
    """Compare two or more companies side by side across growth, profitability, valuation,
    momentum and news — all dimensions fetched at once and returned aligned. Use this for any
    comparison; never call research_company twice to compare.""",
    {
        "type": "object",
        "properties": {
            "symbols": {"type": "array", "items": {"type": "string"}, "description": "Two or more tickers"},
            "dimensions": {
                "type": "array",
                "items": {"type": "string", "enum": list(DIMENSIONS)},
                "description": "Which dimensions to compare. Default: growth, profitability, valuation.",
            },
        },
        "required": ["symbols"],
    },
)
async def compare_companies(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbols = [str(s).upper().strip() for s in (args.get("symbols") or []) if str(s).strip()][:5]
    dims = [d for d in (args.get("dimensions") or []) if d in DIMENSIONS] or [
        "growth", "profitability", "valuation"
    ]
    if len(symbols) < 2:
        return {"error": "need at least two symbols to compare"}

    bundles = await asyncio.gather(*(_bundle(s, set(dims)) for s in symbols))

    rows: dict[str, Any] = {}
    errors: list[str] = []
    for symbol, raw in zip(symbols, bundles):
        info = raw["info"] if isinstance(raw["info"], dict) and "error" not in raw["info"] else {}
        if not info and dims != ["momentum"]:
            errors.append(f"{symbol}: fundamentals unavailable")
        row: dict[str, Any] = {"name": info.get("longName", symbol)}
        if "growth" in dims:
            row["growth"] = growth_of(info, raw["metrics"])
        if "profitability" in dims:
            row["profitability"] = profitability_of(info, raw["metrics"])
        if "valuation" in dims:
            row["valuation"] = valuation_of(info, raw["metrics"])
        if "momentum" in dims:
            row["momentum"] = momentum_of(raw["quote"], raw["hist"])
        if "news" in dims:
            row["news"] = raw["news"] if isinstance(raw["news"], list) else []
        rows[symbol] = row

    data = {"dimensions": dims, "companies": rows, "session": session_state()}
    if errors:
        data["errors"] = errors
    return ok(data, source="Yahoo Finance, Finnhub")


@tool(
    "explain_move",
    """Why a stock moved: the move itself, the news around it, any fresh SEC filing, and how its
    sector did on the same day — joined into one result. Use for "why did X drop/jump".""",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "window": {"type": "string", "enum": ["1d", "5d", "1mo"], "description": "Default 1d"},
        },
        "required": ["symbol"],
    },
)
async def explain_move(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbol = str(args.get("symbol", "")).upper().strip()
    window = args.get("window") or "1d"
    if not symbol:
        return {"error": "no symbol given"}

    days = {"1d": 3, "5d": 7, "1mo": 30}.get(window, 3)
    quote, hist, news, recent_filings, info = await asyncio.gather(
        one_quote(symbol),
        yfc.history(symbol, "1mo"),
        finnhub.company_news(symbol, days=days, limit=6),
        edgar.recent_filings(symbol, forms=["8-K", "10-Q", "10-K"], limit=3),
        yfc.info(symbol),
    )

    sector = (info or {}).get("sector") if isinstance(info, dict) else None
    peer = await one_quote(SECTOR_ETF[sector]) if sector in SECTOR_ETF else {}

    data: dict[str, Any] = {
        "symbol": symbol,
        "window": window,
        "session": session_state(),
        "move": momentum_of(quote, hist),
        "news": news if isinstance(news, list) else [],
        "recent_filings": recent_filings if isinstance(recent_filings, list) else [],
    }
    if sector:
        data["sector_context"] = {
            "sector": sector,
            "sector_etf": peer.get("symbol"),
            "sector_change_pct": peer.get("change_pct"),
        }
    if "error" in quote:
        return {"error": f"couldn't get a quote for {symbol}"}
    return ok(data, source="Yahoo Finance, Finnhub, SEC EDGAR")
