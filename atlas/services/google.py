"""Google OAuth and direct REST clients for Sheets, Gmail, Calendar and Drive."""

import asyncio
import csv
import io
import re
from statistics import mean
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from atlas.config import settings
from atlas.db.integrations import REQUESTED_SCOPES
from atlas.services.util import http

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def authorization_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "response_type": "code",
            "scope": " ".join(REQUESTED_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
    )


async def exchange_code(code: str) -> dict[str, Any]:
    try:
        response = await http().post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        return {"error": f"Google authorization failed ({type(exc).__name__})"}


def parse_spreadsheet(value: str) -> tuple[str, str | None] | None:
    value = value.strip()
    match = SHEET_ID.search(value)
    if match:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        fragment = parse_qs(parsed.fragment)
        return match.group(1), (query.get("gid") or fragment.get("gid") or [None])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value, None
    return None


async def public_sheet(spreadsheet_id: str, gid: str | None = None, range_: str | None = None) -> Any:
    params: dict[str, str] = {"format": "csv"}
    if gid:
        params["gid"] = gid
    if range_:
        params["range"] = range_
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    for attempt in range(2):
        try:
            response = await http().get(url, params=params, timeout=12)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type or response.text.lstrip().startswith("<!DOCTYPE html"):
                return {"error": "sheet is not public"}
            return list(csv.reader(io.StringIO(response.text)))
        except Exception as exc:
            if attempt:
                return {"error": f"public sheet unavailable ({type(exc).__name__})"}
            await asyncio.sleep(0.35)
    return {"error": "public sheet unavailable"}


async def sheet_values(token: str, spreadsheet_id: str, range_: str = "A:Z") -> Any:
    path = quote(range_, safe="!:$'")
    result = await _get_json(
        f"{SHEETS_API}/{spreadsheet_id}/values/{path}", token, "Google Sheets read"
    )
    return result.get("values", []) if isinstance(result, dict) and "error" not in result else result


def sheet_result(rows: list[list[Any]]) -> dict[str, Any]:
    width = max((len(row) for row in rows), default=0)
    cells = sum(len(row) for row in rows)
    if len(rows) <= 100 and cells <= 1500:
        return {"row_count": len(rows), "column_count": width, "rows": rows}

    headers = [str(v) for v in (rows[0] if rows else [])]
    stats: dict[str, dict[str, float]] = {}
    for index, header in enumerate(headers):
        numbers = [_number(row[index]) for row in rows[1:] if index < len(row)]
        clean = [value for value in numbers if value is not None]
        if clean and len(clean) >= max(2, len(numbers) // 2):
            stats[header or f"column_{index + 1}"] = {
                "min": min(clean), "max": max(clean), "mean": round(mean(clean), 4)
            }
    return {
        "row_count": len(rows),
        "column_count": width,
        "headers": headers,
        "numeric_stats": stats,
        "sample": rows[:12],
        "note": "Large sheet summarized; ask for a narrower range to inspect exact rows.",
    }


def _number(value: Any) -> float | None:
    text = str(value).strip().replace(",", "").replace("$", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


async def spreadsheet_titles(token: str, spreadsheet_id: str) -> Any:
    result = await _get_json(
        f"{SHEETS_API}/{spreadsheet_id}", token, "Google Sheets metadata", params={"fields": "sheets.properties.title"}
    )
    if "error" in result:
        return result
    return [sheet.get("properties", {}).get("title", "") for sheet in result.get("sheets", [])]


async def add_sheet(token: str, spreadsheet_id: str, title: str) -> dict[str, Any]:
    return await _write_json(
        "POST", f"{SHEETS_API}/{spreadsheet_id}:batchUpdate", token,
        "create sheet tab", json={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    )


async def update_values(token: str, spreadsheet_id: str, range_: str, values: list[list[Any]]) -> dict[str, Any]:
    path = quote(range_, safe="!:$'")
    return await _write_json(
        "PUT", f"{SHEETS_API}/{spreadsheet_id}/values/{path}", token,
        "Google Sheets write", params={"valueInputOption": "USER_ENTERED"},
        json={"range": range_, "majorDimension": "ROWS", "values": values},
    )


async def gmail_search(token: str, query: str, maximum: int = 10) -> Any:
    listing = await _get_json(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages", token, "Gmail search",
        params={"q": query, "maxResults": max(1, min(maximum, 20))},
    )
    if "error" in listing:
        return listing
    ids = [item.get("id") for item in listing.get("messages", []) if item.get("id")]
    messages = await asyncio.gather(*(_gmail_message(token, message_id) for message_id in ids))
    return [message for message in messages if "error" not in message]


async def _gmail_message(token: str, message_id: str) -> dict[str, Any]:
    data = await _get_json(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}", token,
        "Gmail message", params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
    )
    if "error" in data:
        return data
    headers = {h.get("name", "").lower(): h.get("value", "") for h in data.get("payload", {}).get("headers", [])}
    return {"id": message_id, "thread_id": data.get("threadId"), "subject": headers.get("subject", ""),
            "from": headers.get("from", ""), "date": headers.get("date", ""), "snippet": data.get("snippet", "")}


async def calendar_events(token: str, start: str, end: str) -> Any:
    data = await _get_json(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events", token, "Google Calendar",
        params={"timeMin": start, "timeMax": end, "singleEvents": "true", "orderBy": "startTime", "maxResults": 30},
    )
    if "error" in data:
        return data
    return [{"summary": e.get("summary", ""), "start": e.get("start", {}), "end": e.get("end", {}),
             "attendees": [a.get("email", "") for a in e.get("attendees", [])], "location": e.get("location", "")}
            for e in data.get("items", [])]


async def drive_search(token: str, query: str, maximum: int = 10) -> Any:
    escaped = query.replace("'", "\\'")
    data = await _get_json(
        "https://www.googleapis.com/drive/v3/files", token, "Google Drive search",
        params={"q": f"trashed = false and fullText contains '{escaped}'", "pageSize": max(1, min(maximum, 20)),
                "fields": "files(id,name,mimeType,modifiedTime,webViewLink,description)"},
    )
    return data.get("files", []) if "error" not in data else data


async def _get_json(url: str, token: str, what: str, params: Any = None) -> dict[str, Any]:
    for attempt in range(2):
        try:
            response = await http().get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt:
                return {"error": f"{what} unavailable ({type(exc).__name__})"}
            await asyncio.sleep(0.35)
    return {"error": f"{what} unavailable"}


async def _write_json(method: str, url: str, token: str, what: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = await http().request(method, url, headers={"Authorization": f"Bearer {token}"}, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        return {"error": f"{what} failed ({type(exc).__name__})"}
