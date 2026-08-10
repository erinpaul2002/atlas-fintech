"""Google Calendar event creation with deterministic duplicate protection."""

import re
from typing import Any
from urllib.parse import quote

from atlas.services.util import http

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


async def create_calendar_event(
    token: str,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    attendees: list[str] | None = None,
    location: str = "",
    timezone: str = "",
    reminder_minutes: list[int] | None = None,
    event_id: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": summary,
        "start": _calendar_time(start, timezone),
        "end": _calendar_time(end, timezone),
    }
    if event_id:
        body["id"] = event_id
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    clean_attendees = [email.strip() for email in attendees or [] if email.strip()]
    if clean_attendees:
        body["attendees"] = [{"email": email} for email in clean_attendees]
    if reminder_minutes:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": int(minutes)}
                for minutes in reminder_minutes
            ],
        }

    try:
        response = await http().post(
            EVENTS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"sendUpdates": "all" if clean_attendees else "none"},
            json=body,
            timeout=20,
        )
        if response.status_code == 409 and event_id:
            return await _existing_event(token, event_id)
        response.raise_for_status()
        return _event_result(response.json())
    except Exception as exc:
        return {"error": f"Google Calendar create failed ({type(exc).__name__})"}


async def _existing_event(token: str, event_id: str) -> dict[str, Any]:
    try:
        response = await http().get(
            f"{EVENTS_URL}/{quote(event_id, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
        return _event_result(response.json())
    except Exception as exc:
        return {"error": f"Google Calendar event recovery failed ({type(exc).__name__})"}


def _calendar_time(value: str, timezone: str) -> dict[str, str]:
    result = {"date": value} if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else {"dateTime": value}
    if timezone and "dateTime" in result:
        result["timeZone"] = timezone
    return result


def _event_result(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id", ""),
        "summary": event.get("summary", ""),
        "start": event.get("start", {}),
        "end": event.get("end", {}),
        "attendees": [item.get("email", "") for item in event.get("attendees", [])],
        "html_link": event.get("htmlLink", ""),
        "status": event.get("status", "confirmed"),
    }
