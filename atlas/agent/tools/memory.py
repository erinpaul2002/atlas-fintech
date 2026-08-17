"""Memory, watchlist, alerts, profile. The tools that make the bot feel like it knows them."""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.db import alerts as alerts_repo
from atlas.db import facts as facts_repo
from atlas.db import users as users_repo
from atlas.db.client import db
from atlas.services import yfinance_client as yfc


@tool(
    "remember",
    """Store something durable about this person — their role, holdings, sectors they follow,
    how they like to work, a view they hold. Call it silently the moment they reveal something;
    never announce it and never ask permission. One claim per call.""",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The fact, in one short sentence, third person"},
            "kind": {
                "type": "string",
                "enum": ["interest", "position", "preference", "context", "person"],
            },
        },
        "required": ["text", "kind"],
    },
)
async def remember(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text", "")).strip()
    if not text:
        return {"error": "nothing to remember"}
    await facts_repo.add(ctx.user.id, text=text, kind=str(args.get("kind", "context")))
    return ok({"stored": text}, source="memory")


@tool(
    "recall",
    """Search conversations older than the recent window for something they said or asked
    before. Use only when they refer to something beyond the last few days — recent turns and
    current facts are already in front of you.""",
    {
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "What to look for"}},
        "required": ["topic"],
    },
)
async def recall(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    topic = str(args.get("topic", "")).strip()
    if not topic:
        return {"error": "no topic given"}
    # ponytail: keyword scan over this user's own messages. Vector recall (DATA_MODEL.md
    # "Vector indexes") replaces this once digest_embedding is populated — same signature.
    terms = [t for t in re.findall(r"[A-Za-z]{3,}", topic)][:4]
    if not terms:
        return {"error": "no topic given"}
    pattern = "|".join(re.escape(t) for t in terms)
    cursor = (
        db().messages.find(
            {"user_id": ctx.user.id, "content": {"$regex": pattern, "$options": "i"},
             "role": {"$in": ["user", "assistant"]}}
        )
        .sort("created_at", -1)
        .limit(8)
    )
    hits = [
        {"when": d["created_at"].date().isoformat(), "who": d["role"], "said": d["content"][:400]}
        async for d in cursor
    ]
    if not hits:
        return ok({"matches": [], "note": "nothing in earlier conversation about that"}, source="conversation history")
    return ok({"matches": hits}, source="conversation history")


@tool(
    "manage_watchlist",
    """Add, remove or list what they're following. Always store why they're watching it — the
    reason is what makes later alerts and briefs read like they were written for them.""",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "symbol": {"type": "string"},
            "reason": {"type": "string", "description": "Why they care about it, in their words"},
        },
        "required": ["action"],
    },
)
async def manage_watchlist(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "list")).lower()
    symbol = str(args.get("symbol") or "").upper().strip()

    if action == "list":
        user = await users_repo.by_id(ctx.user.id)
        items = [w.model_dump(exclude={"added_at"}) for w in (user.watchlist if user else [])]
        return ok({"watchlist": items}, source="their watchlist")

    if not symbol:
        return {"error": "no symbol given"}

    if action == "remove":
        user = await users_repo.remove_from_watchlist(ctx.user.id, symbol)
        return ok({"removed": symbol, "watchlist": [w.symbol for w in (user.watchlist if user else [])]},
                  source="their watchlist")

    info = await yfc.info(symbol)
    if "error" in info:
        quote = await yfc.quote(symbol)
        if "error" in quote:
            return {"error": f"{symbol} doesn't look like a tradeable symbol"}
    name = info.get("longName", symbol) if isinstance(info, dict) else symbol
    user = await users_repo.add_to_watchlist(ctx.user.id, symbol, name, str(args.get("reason") or ""))
    return ok(
        {"added": symbol, "company": name, "watchlist": [w.symbol for w in (user.watchlist if user else [])]},
        source="their watchlist",
    )


@tool(
    "manage_alerts",
    """Create, list or cancel a background watch: a price move threshold, new SEC filings, news
    on a company, an earnings reminder, or a one-time/daily reminder at a specific local time.
    Store their exact wording — listing alerts reads it back to them verbatim. Never turn a
    clock-time reminder into a price_move alert.""",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "cancel"]},
            "kind": {
                "type": "string",
                "enum": ["price_move", "filing", "news", "earnings", "time"],
            },
            "symbol": {"type": "string"},
            "params": {
                "type": "object",
                "description": "price_move: {pct, direction: up|down|any}; filing: {forms: []}; "
                "earnings: {offset_minutes}; time: either {at, timezone, recurring: once} or "
                "{hour_local, minute_local, timezone, recurring: daily, message}",
                "properties": {
                    "pct": {"type": "number"},
                    "direction": {"type": "string", "enum": ["up", "down", "any"]},
                    "forms": {"type": "array", "items": {"type": "string"}},
                    "offset_minutes": {"type": "integer"},
                    "at": {"type": "string", "description": "RFC 3339 time for a one-time reminder"},
                    "hour_local": {"type": "integer", "minimum": 0, "maximum": 23},
                    "minute_local": {"type": "integer", "minimum": 0, "maximum": 59},
                    "timezone": {"type": "string", "description": "IANA timezone, e.g. Asia/Kolkata"},
                    "recurring": {"type": "string", "enum": ["once", "daily"]},
                    "message": {"type": "string"},
                },
            },
            "natural_language": {"type": "string", "description": "What they asked for, in their own words"},
        },
        "required": ["action"],
    },
)
async def manage_alerts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action", "list")).lower()

    if action == "list":
        rows = await alerts_repo.for_user(ctx.user.id)
        return ok(
            {"alerts": [{"id": str(a.id), "kind": a.kind, "symbol": a.symbol,
                         "their_words": a.natural_language} for a in rows]},
            source="their alerts",
        )

    if action == "cancel":
        n = await alerts_repo.cancel(ctx.user.id, symbol=args.get("symbol"))
        return ok({"cancelled": n}, source="their alerts")

    kind = str(args.get("kind") or "news")
    params = dict(args.get("params") or {})
    if kind == "price_move":
        try:
            threshold = float(params.get("pct") or 0)
        except (TypeError, ValueError):
            threshold = 0
        if threshold <= 0:
            return {"error": "A price alert needs a positive percentage threshold."}
        params["pct"] = threshold
        params["direction"] = str(params.get("direction") or "any")
    elif kind == "time":
        params, error = _time_alert_params(params, ctx.user.timezone)
        if error:
            return {"error": error}

    alert = await alerts_repo.create(
        ctx.user.id,
        kind=kind,
        symbol=args.get("symbol"),
        params=params,
        natural_language=str(args.get("natural_language") or ""),
    )
    return ok(
        {"created": {"id": str(alert.id), "kind": alert.kind, "symbol": alert.symbol,
                     "their_words": alert.natural_language}},
        source="their alerts",
    )


def _time_alert_params(
    params: dict[str, Any], user_timezone: str
) -> tuple[dict[str, Any], str | None]:
    """Validate and normalize time alerts before they become durable promises."""
    timezone_name = str(params.get("timezone") or user_timezone or "UTC").strip()
    try:
        zone = ZoneInfo(timezone_name)
    except (KeyError, ValueError):
        return {}, f"Unknown timezone: {timezone_name}."
    recurring = str(params.get("recurring") or ("once" if params.get("at") else "daily")).lower()
    if recurring not in {"once", "daily"}:
        return {}, "A time alert must recur either once or daily."

    normalized: dict[str, Any] = {
        "timezone": timezone_name,
        "recurring": recurring,
        "message": str(params.get("message") or "Scheduled reminder").strip(),
        # A sleeping host can still deliver shortly after it wakes up.
        "grace_minutes": 180,
    }
    if params.get("at"):
        try:
            raw = str(params["at"]).strip().replace("Z", "+00:00")
            at = datetime.fromisoformat(raw)
            if at.tzinfo is None:
                at = at.replace(tzinfo=zone)
        except (TypeError, ValueError):
            return {}, "A one-time alert needs a valid RFC 3339 date and time."
        if recurring != "once":
            return {}, "Use a local hour and minute for a daily alert."
        normalized["at"] = at.isoformat()
        return normalized, None

    try:
        hour = int(params["hour_local"])
        minute = int(params.get("minute_local") or 0)
    except (KeyError, TypeError, ValueError):
        return {}, "A time alert needs a local hour (and optional minute)."
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return {}, "The reminder time must use hour 0-23 and minute 0-59."
    normalized.update({"hour_local": hour, "minute_local": minute})
    return normalized, None


@tool(
    "update_profile",
    """Update structured things they state or change: their role, timezone, what time the
    morning brief should land, sectors and topics they follow, what's worth interrupting them
    for, tone and depth preferences.""",
    {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "timezone": {"type": "string", "description": "IANA name, e.g. Asia/Kolkata"},
            "brief_hour_local": {"type": "integer", "description": "0-23, or -1 for no brief"},
            "brief_minute_local": {"type": "integer"},
            "sectors": {"type": "array", "items": {"type": "string"}},
            "topics": {"type": "array", "items": {"type": "string"}},
            "markets": {"type": "array", "items": {"type": "string"}},
            "intel_preferences": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What's worth interrupting them for, e.g. ['earnings', 'filings']",
            },
            "tone": {"type": "string"},
            "depth": {"type": "string"},
        },
    },
)
async def update_profile(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    user = ctx.user
    patch: dict[str, Any] = {}
    for key in ("role", "timezone", "brief_minute_local", "intel_preferences"):
        if args.get(key) is not None:
            patch[key] = args[key]

    if args.get("brief_hour_local") is not None:
        hour = int(args["brief_hour_local"])
        patch["brief_hour_local"] = None if hour < 0 else max(0, min(23, hour))

    interests = user.interests.model_dump()
    for key in ("sectors", "topics", "markets"):
        if args.get(key):
            merged = list(dict.fromkeys([*interests[key], *[str(v) for v in args[key]]]))
            interests[key] = merged
    if any(args.get(k) for k in ("sectors", "topics", "markets")):
        patch["interests"] = interests

    prefs = dict(user.preferences)
    for key in ("tone", "depth"):
        if args.get(key):
            prefs[key] = args[key]
    if prefs != user.preferences:
        patch["preferences"] = prefs

    if not patch:
        return {"error": "nothing to update"}
    updated = await users_repo.patch(user.id, patch)
    if updated:
        ctx.user = updated
    return ok({"updated": sorted(patch.keys())}, source="their profile")
