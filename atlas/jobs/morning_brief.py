"""Personalized, relevance-gated morning brief delivery."""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from atlas.agent.tools.market import quotes_for
from atlas.bot.outbound import send_text
from atlas.db import deliveries, facts as facts_repo, messages as messages_repo, seen, users
from atlas.db.models import User, utcnow
from atlas.jobs.relevance import filing_score, keep, news_score, price_score
from atlas.llm import provider
from atlas.services import edgar, finnhub

SECTOR_SYMBOLS = {
    "technology": "XLK", "financial services": "XLF", "financials": "XLF", "energy": "XLE",
    "healthcare": "XLV", "health care": "XLV", "consumer cyclical": "XLY",
    "industrials": "XLI", "semiconductors": "SMH", "real estate": "XLRE",
    "utilities": "XLU", "communication services": "XLC", "materials": "XLB",
}
MARKET_SYMBOLS = {
    "us": "^GSPC", "s&p 500": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI",
}

log = logging.getLogger(__name__)


async def run(bot: Any, now: datetime | None = None) -> None:
    now = (now or utcnow()).astimezone(timezone.utc)
    for user in await users.with_briefs_enabled():
        try:
            await _run_user(bot, user, now)
        except Exception as exc:
            # One malformed timezone/provider result must not suppress every other user's brief.
            log.warning("morning brief failed for user %s: %s", user.id, exc)
            continue


async def _run_user(bot: Any, user: User, now: datetime) -> None:
    zone = _zone(user.timezone)
    local = now.astimezone(zone)
    due_at = time(user.brief_hour_local or 0, user.brief_minute_local)
    if local.time().replace(tzinfo=None) < due_at:
        return
    start_local = datetime.combine(local.date(), time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    if await deliveries.exists_between(
        user.id, "morning_brief", start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    ):
        return

    candidates = await gather(user, now)
    novel = [candidate for candidate in candidates if not await seen.contains(user.id, candidate["key"])]
    selected = keep(novel, limit=5)
    if len(selected) < 2:
        await deliveries.record(
            user.id, "morning_brief", item_count=0, candidates_considered=len(candidates)
        )
        return

    brief = await _write(user, selected)
    if not brief or not await send_text(bot, user.telegram_chat_id, brief):
        return
    for candidate in selected:
        await seen.mark(user.id, candidate["key"])
    await deliveries.record(
        user.id, "morning_brief", item_count=len(selected), candidates_considered=len(candidates)
    )


async def gather(user: User, now: datetime) -> list[dict[str, Any]]:
    watch_symbols = [item.symbol for item in user.watchlist[:8]]
    interest_symbols = [
        SECTOR_SYMBOLS[name.lower()]
        for name in user.interests.sectors
        if name.lower() in SECTOR_SYMBOLS
    ] + [
        MARKET_SYMBOLS[name.lower()]
        for name in user.interests.markets
        if name.lower() in MARKET_SYMBOLS
    ]
    symbols = list(dict.fromkeys(watch_symbols + interest_symbols))[:10]
    if not symbols:
        return []
    quotes, *bundles = await asyncio.gather(
        quotes_for(symbols),
        *(_company_candidates(symbol, now) for symbol in watch_symbols),
    )
    local_day = now.astimezone(_zone(user.timezone)).date().isoformat()
    candidates = [candidate for bundle in bundles for candidate in bundle]
    for quote in quotes:
        change = quote.get("change_pct")
        symbol = quote.get("symbol") or ""
        score = price_score(change, on_watchlist=symbol in watch_symbols)
        if score < 4:
            continue
        candidates.append(
            {
                "key": f"price:{symbol}:{local_day}",
                "score": score,
                "text": f"{symbol} is {float(change):+.2f}% at {quote.get('price')}",
                "source": quote.get("source", "Yahoo Finance"),
                "as_of": quote.get("as_of", now.isoformat()),
            }
        )
    return candidates


async def _company_candidates(symbol: str, now: datetime) -> list[dict[str, Any]]:
    news, filings, earnings = await asyncio.gather(
        finnhub.company_news(symbol, days=2, limit=5),
        edgar.recent_filings(symbol, forms=["8-K", "10-Q", "10-K", "6-K"], limit=4),
        finnhub.earnings_calendar(symbol=symbol, days_ahead=3, days_back=0),
    )
    out: list[dict[str, Any]] = []
    cutoff = (now - timedelta(hours=36)).timestamp()
    if isinstance(news, list):
        for item in news:
            if float(item.get("datetime") or 0) < cutoff:
                continue
            value = str(item.get("url") or item.get("headline") or "")
            out.append(
                {
                    "key": "news:" + hashlib.sha1(value.encode()).hexdigest(),
                    "score": news_score(item),
                    "text": f"{symbol}: {item.get('headline')}",
                    "source": item.get("source") or "Finnhub",
                    "as_of": datetime.fromtimestamp(float(item.get("datetime") or 0), timezone.utc).isoformat(),
                }
            )
    if isinstance(filings, list):
        cutoff_day = (now - timedelta(days=1)).date().isoformat()
        for item in filings:
            if item.get("filed_at", "") < cutoff_day:
                continue
            out.append(
                {
                    "key": f"filing:{item['accession']}",
                    "score": filing_score(item["form"]),
                    "text": f"{symbol} filed {item['form']}: {item['title']}",
                    "source": "SEC EDGAR",
                    "as_of": item["filed_at"],
                }
            )
    if isinstance(earnings, list):
        for item in earnings:
            if not item.get("date") or item["date"] < now.date().isoformat():
                continue
            out.append(
                {
                    "key": f"earnings:{symbol}:{item['date']}",
                    "score": 4.5,
                    "text": f"{symbol} reports earnings {item['date']} ({item.get('hour') or 'time unconfirmed'})",
                    "source": "Finnhub",
                    "as_of": now.isoformat(),
                }
            )
            break
    return out


async def _write(user: User, selected: list[dict[str, Any]]) -> str:
    facts = json.dumps(selected, ensure_ascii=False, default=str)
    recent_facts, recent_turns = await asyncio.gather(
        facts_repo.current(user.id, limit=8),
        messages_repo.recent(user.id, limit=6),
    )
    context = {
        "profile": user.profile_narrative,
        "watch_reasons": [item.reason for item in user.watchlist if item.reason],
        "recent_facts": [fact.text for fact in recent_facts],
        "recent_conversation": [turn.content[:240] for turn in recent_turns],
    }
    draft = await provider.generate(
        system=(
            "Write a Telegram morning brief for a finance professional. Use only the supplied facts. "
            "Rank the most important first. Write 3-5 short lines, no greeting, header, table, or advice. "
            "Each line must name its source and as-of date/time. Connect items to the user context only "
            "when supported."
        ),
        prompt=f"User context: {json.dumps(context, ensure_ascii=False)}\nCandidates: {facts}",
        temperature=0.25,
    )
    if not draft.strip():
        return ""
    polished = await provider.generate(
        system=(
            "Edit this finance brief. Preserve every factual claim and citation. Return at most five "
            "short lines, biggest first, no greeting, heading, table, disclaimer, or sign-off."
        ),
        prompt=draft,
        temperature=0.1,
    )
    return (polished or draft).strip()


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
