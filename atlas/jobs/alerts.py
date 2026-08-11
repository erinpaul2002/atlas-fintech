"""Evaluate durable time, price, filing, news, and earnings alerts."""

import asyncio
import hashlib
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from atlas.bot.outbound import send_text
from atlas.db import alerts as alerts_repo
from atlas.db import deliveries, seen, users
from atlas.db.models import Alert, utcnow
from atlas.jobs.relevance import news_score
from atlas.services import edgar, finnhub

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


async def run(bot: Any, now: datetime | None = None) -> None:
    # Imported lazily so this job module remains independently testable while the tool registry
    # is populating (market and research tools refer to each other through that registry).
    from atlas.agent.tools.market import session_state

    now = (now or utcnow()).astimezone(timezone.utc)
    jobs = [run_time_alerts(bot, now), run_earnings(bot, now)]
    if session_state(now) == "market open":
        jobs.append(run_prices(bot, now))
    if now.minute % 15 == 0:
        jobs.extend((run_filings(bot, now), run_news(bot, now)))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            log.warning("alert sweep failed: %s", result)


def price_matches(alert: Alert, change_pct: float | int | None) -> bool:
    if change_pct is None:
        return False
    threshold = abs(float(alert.params.get("pct") or 0))
    direction = str(alert.params.get("direction") or "any")
    change = float(change_pct)
    if threshold <= 0:
        return False
    return (
        (direction == "up" and change >= threshold)
        or (direction == "down" and change <= -threshold)
        or (direction == "any" and abs(change) >= threshold)
    )


def time_alert_due(alert: Alert, now: datetime) -> datetime | None:
    """Return the occurrence this sweep should deliver, including short wake-up catch-ups."""
    now = now.astimezone(timezone.utc)
    params = alert.params
    try:
        grace = timedelta(minutes=max(1, int(params.get("grace_minutes") or 180)))
        zone = ZoneInfo(str(params.get("timezone") or "UTC"))
        if params.get("at"):
            raw = str(params["at"]).replace("Z", "+00:00")
            due = datetime.fromisoformat(raw)
            due = due.replace(tzinfo=zone) if due.tzinfo is None else due
            due = due.astimezone(timezone.utc)
        else:
            hour = int(params["hour_local"])
            minute = int(params.get("minute_local") or 0)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return None
            local_now = now.astimezone(zone)
            local_due = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
            if local_due > local_now:
                local_due -= timedelta(days=1)
            due = local_due.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None

    created_at = alert.created_at.astimezone(timezone.utc)
    last_fired = alert.last_fired_at.astimezone(timezone.utc) if alert.last_fired_at else None
    if created_at > due or (last_fired and last_fired >= due):
        return None
    return due if due <= now <= due + grace else None


async def run_time_alerts(bot: Any, now: datetime) -> None:
    for alert in await alerts_repo.due("time"):
        if time_alert_due(alert, now) is None:
            continue
        message = str(alert.params.get("message") or alert.natural_language or "Scheduled reminder")
        if await _deliver(bot, alert, f"**Reminder:** {message}"):
            if str(alert.params.get("recurring") or "once") != "daily":
                await alerts_repo.deactivate(alert.id)


async def run_prices(bot: Any, now: datetime) -> None:
    from atlas.agent.tools.market import quotes_for

    alerts = [alert for alert in await alerts_repo.due("price_move") if alert.symbol]
    symbols = list(dict.fromkeys(alert.symbol for alert in alerts if alert.symbol))
    quotes = {row.get("symbol"): row for row in await quotes_for(symbols)}
    for alert in alerts:
        quote = quotes.get(alert.symbol) or {}
        if _fired_today(alert, now) or not price_matches(alert, quote.get("change_pct")):
            continue
        change = float(quote["change_pct"])
        direction = "up" if change >= 0 else "down"
        message = (
            f"{alert.symbol} just crossed your alert: **{direction} {abs(change):.2f}%** today "
            f"at {quote.get('price')}. ({quote.get('source', 'Yahoo Finance')}, {quote.get('as_of', '')})"
        )
        await _deliver(bot, alert, message)


async def run_filings(bot: Any, now: datetime) -> None:
    alerts = [alert for alert in await alerts_repo.due("filing") if alert.symbol]
    grouped = _group(alerts)
    for symbol, symbol_alerts in grouped.items():
        forms = sorted({str(form).upper() for alert in symbol_alerts for form in alert.params.get("forms", [])})
        result = await edgar.recent_filings(symbol, forms=forms or None, limit=12)
        if not isinstance(result, list):
            continue
        for alert in symbol_alerts:
            wanted = {str(form).upper() for form in alert.params.get("forms", [])}
            since = (alert.last_checked_at or alert.created_at).date().isoformat()
            matches = [item for item in result if item["filed_at"] >= since and
                       (not wanted or item["form"].upper() in wanted)]
            for item in matches[:1]:
                key = f"filing:{item['accession']}"
                if await seen.contains(alert.user_id, key):
                    continue
                message = (
                    f"{symbol} filed a new **{item['form']}** on {item['filed_at']}: "
                    f"{item['title']}. (SEC EDGAR)"
                )
                if await _deliver(bot, alert, message):
                    await seen.mark(alert.user_id, key)
            await alerts_repo.mark_checked(alert.id)


async def run_news(bot: Any, now: datetime) -> None:
    alerts = [alert for alert in await alerts_repo.due("news") if alert.symbol]
    grouped = _group(alerts)
    for symbol, symbol_alerts in grouped.items():
        result = await finnhub.company_news(symbol, days=2, limit=12)
        if not isinstance(result, list):
            continue
        for alert in symbol_alerts:
            since = (alert.last_checked_at or alert.created_at).timestamp()
            matches = [item for item in result if float(item.get("datetime") or 0) >= since]
            matches.sort(key=news_score, reverse=True)
            for item in matches[:1]:
                key = _news_key(item)
                if news_score(item) < 5 or await seen.contains(alert.user_id, key):
                    continue
                message = f"{symbol}: **{item.get('headline', 'Material update')}** ({item.get('source') or 'Finnhub'})"
                if await _deliver(bot, alert, message):
                    await seen.mark(alert.user_id, key)
            await alerts_repo.mark_checked(alert.id)


async def run_earnings(bot: Any, now: datetime) -> None:
    alerts = [alert for alert in await alerts_repo.due("earnings") if alert.symbol and not alert.last_fired_at]
    grouped = _group(alerts)
    for symbol, symbol_alerts in grouped.items():
        result = await finnhub.earnings_calendar(symbol=symbol, days_ahead=14, days_back=0)
        if not isinstance(result, list):
            continue
        events = sorted((item for item in result if item.get("date")), key=lambda item: item["date"])
        for alert in symbol_alerts:
            offset = max(0, int(alert.params.get("offset_minutes") or 60))
            event = next((item for item in events if _earnings_at(item) > now), None)
            if not event:
                continue
            event_at = _earnings_at(event)
            if now < event_at - timedelta(minutes=offset):
                continue
            label = {"bmo": "before market open", "amc": "after market close"}.get(
                str(event.get("hour") or "").lower(), "during the session"
            )
            message = f"{symbol} reports earnings **{label} today** ({event['date']}, Finnhub)."
            if await _deliver(bot, alert, message):
                await alerts_repo.deactivate(alert.id)


def _earnings_at(event: dict[str, Any]) -> datetime:
    day = datetime.fromisoformat(str(event["date"])).date()
    clock = {"bmo": time(8), "amc": time(16, 15), "dmh": time(12)}.get(
        str(event.get("hour") or "").lower(), time(12)
    )
    return datetime.combine(day, clock, tzinfo=ET).astimezone(timezone.utc)


def _group(alerts: list[Alert]) -> dict[str, list[Alert]]:
    grouped: dict[str, list[Alert]] = {}
    for alert in alerts:
        grouped.setdefault(alert.symbol or "", []).append(alert)
    return grouped


def _fired_today(alert: Alert, now: datetime) -> bool:
    return bool(alert.last_fired_at and alert.last_fired_at.astimezone(ET).date() == now.astimezone(ET).date())


def _news_key(item: dict[str, Any]) -> str:
    value = str(item.get("url") or item.get("headline") or "")
    return "news:" + hashlib.sha1(value.encode()).hexdigest()


async def _deliver(bot: Any, alert: Alert, message: str) -> bool:
    user = await users.by_id(alert.user_id)
    if not user or not user.telegram_chat_id:
        return False
    if not await send_text(bot, user.telegram_chat_id, message):
        return False
    await alerts_repo.mark_fired(alert.id)
    await deliveries.record(alert.user_id, "alert", ref_id=alert.id, item_count=1, candidates_considered=1)
    return True
