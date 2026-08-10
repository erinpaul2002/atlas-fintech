"""Google Calendar write proposal. Execution remains private to the action layer."""

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from atlas.agent import actions
from atlas.agent.tools.google_auth import not_connected
from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.db import integrations


@tool(
    "propose_calendar_event",
    "Propose, but never create, a Google Calendar meeting or reminder. Use RFC3339 times and stop for explicit confirmation after this tool.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Calendar event title"},
            "start": {
                "type": "string",
                "description": "RFC3339 start with offset, or YYYY-MM-DD for all-day",
            },
            "end": {
                "type": "string",
                "description": "RFC3339 end with offset, or exclusive YYYY-MM-DD for all-day",
            },
            "timezone": {"type": "string", "description": "IANA timezone when times lack offsets"},
            "description": {"type": "string"},
            "location": {"type": "string"},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses that will receive invitations",
            },
            "reminder_minutes": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Popup reminders in minutes before the event",
            },
        },
        "required": ["summary", "start", "end"],
    },
)
async def propose_calendar_event(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.CALENDAR_SCOPE)
    if not token:
        return await not_connected(ctx)

    summary = str(args.get("summary") or "").strip()
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    timezone = str(args.get("timezone") or ctx.user.timezone or "UTC").strip()
    error = _validate_event(summary, start, end, timezone)
    if error:
        return {"error": error}

    attendees = list(dict.fromkeys(
        str(value).strip().lower() for value in args.get("attendees") or [] if str(value).strip()
    ))
    if len(attendees) > 50 or any(not _looks_like_email(value) for value in attendees):
        return {"error": "Calendar attendees must be at most 50 valid email addresses."}

    try:
        reminders = sorted(set(int(value) for value in args.get("reminder_minutes") or []))
    except (TypeError, ValueError):
        return {"error": "Calendar reminders must be whole minutes before the event."}
    if len(reminders) > 5 or any(value < 0 or value > 40_320 for value in reminders):
        return {"error": "Use at most five reminders between 0 minutes and 4 weeks before."}

    spec = {
        "summary": summary[:300],
        "start": start,
        "end": end,
        "timezone": timezone,
        "description": str(args.get("description") or "").strip()[:5000],
        "location": str(args.get("location") or "").strip()[:500],
        "attendees": attendees,
        "reminder_minutes": reminders,
    }
    pending = await actions.propose(ctx.user.id, spec, kind="calendar_event")
    extra = {"pending_action_id": str(pending.id)} if pending.status == "pending" else {}
    return ok(
        {"summary": pending.summary, "status": pending.status},
        source="pending action",
        **extra,
    )


def _validate_event(summary: str, start: str, end: str, timezone: str) -> str | None:
    if not summary:
        return "Calendar event needs a title."
    if len(summary) > 300:
        return "Calendar event title is too long."
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return "Calendar event timezone must be a valid IANA timezone such as Asia/Kolkata."
    try:
        start_value, start_all_day = _event_time(start, zone)
        end_value, end_all_day = _event_time(end, zone)
    except ValueError:
        return "Calendar start and end must be RFC3339 times or YYYY-MM-DD all-day dates."
    if start_all_day != end_all_day:
        return "Calendar start and end must both be timed or both be all-day dates."
    if end_value <= start_value:
        return "Calendar event end must be after its start."
    return None


def _event_time(value: str, zone: ZoneInfo) -> tuple[date | datetime, bool]:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value), True
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed, False


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))
