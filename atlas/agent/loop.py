"""The tool-calling loop. One loop, no router, no sub-agents — ARCHITECTURE.md §4.

Independent calls in a round run concurrently. A tool failure becomes `{"error": ...}` inside
the conversation so the model explains the gap; nothing raises into the chat.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.genai import types

from atlas.agent import context as context_builder
from atlas.agent.tools import registry
from atlas.db.models import User
from atlas.llm import provider

log = logging.getLogger(__name__)

MAX_ROUNDS = 5
EMPTY_REPLY = "I couldn't put that together just now — try me again in a moment."


@dataclass
class TurnResult:
    text: str = ""
    rounds: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    provider: str = "gemini"
    total_ms: int = 0
    proposed_action_id: Any = None
    photos: list[dict[str, Any]] = field(default_factory=list)
    visual_links: list[dict[str, str]] = field(default_factory=list)
    resource_links: list[dict[str, str]] = field(default_factory=list)
    collection_results: list[dict[str, Any]] = field(default_factory=list)


async def run_turn(
    user: User,
    turn: types.Content,
    on_delta=None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    connected: bool = False,
    pending_summary: str = "",
    first_turn: bool = False,
    history: list[types.Content] | None = None,
) -> TurnResult:
    started = time.monotonic()
    system = await context_builder.system_instruction(
        user, connected=connected, pending_summary=pending_summary, first_turn=first_turn
    )
    contents = list(history if history is not None else await context_builder.history(user))
    contents.append(turn)

    ctx = registry.ToolContext(user=user, extra={"telegram_photos": []})
    tools = registry.declarations()
    out = TurnResult()
    result = provider.Result()

    for round_no in range(MAX_ROUNDS):
        out.rounds = round_no + 1
        result = await provider.stream(system, contents, tools, on_delta=on_delta)
        out.provider = result.provider
        _add_tokens(out, result)

        if not result.function_calls:
            out.text = result.text
            break

        contents.append(_model_turn(result))
        if on_progress:
            await on_progress(_tool_progress(result.function_calls))
        responses = await _run_tools(result.function_calls, ctx, out)
        if on_progress:
            await on_progress("Preparing your answer…")
        contents.append(types.Content(role="user", parts=responses))
        out.text = ""  # this round was tool traffic; the answer comes from the next one
    else:
        # burned every round on tools — answer with what we have, no tools offered
        log.info("hit the round ceiling for user %s", user.id)
        final = await provider.stream(system, contents, None, on_delta=on_delta)
        out.text = final.text
        _add_tokens(out, final)

    if not out.text.strip():
        out.text = EMPTY_REPLY if result.error or not result.text else result.text
    out.text = _ensure_visual_links(out.text, out.visual_links)
    out.text = _ensure_resource_links(out.text, out.resource_links)
    out.text = _repair_collection_dump(out.text, out.collection_results, out.tool_calls, user)
    out.photos = list(ctx.extra.get("telegram_photos") or [])
    out.total_ms = int((time.monotonic() - started) * 1000)
    return out


def _tool_progress(calls: list[types.FunctionCall]) -> str:
    """Translate internal tool names into honest, user-facing activity labels."""
    names = {str(call.name or "") for call in calls}
    if names & {"render_market_image", "render_visual"}:
        return "Generating the visual…"
    if names & {"read_sheet", "propose_sheet_write", "search_email", "get_calendar",
                "search_drive", "get_email_detail", "read_drive_file", "propose_calendar_event"}:
        return "Working with your Google data…"
    if names & {"filings"}:
        return "Checking SEC filings…"
    if names & {"get_quote", "market_snapshot", "research_company", "compare_companies",
                "explain_move"}:
        return "Fetching live market data…"
    if names & {"web_search"}:
        return "Researching current information…"
    if names & {"recall"}:
        return "Checking our earlier conversations…"
    return "Working through the details…"


def _model_turn(result: provider.Result) -> types.Content:
    """Replay the model's own turn back to it — its Parts, not copies of them.

    `call_parts` carries each function call exactly as it arrived, thought signature included,
    which is what Gemini 2.5 expects to get back. Only fall back to rebuilding if a provider
    gave us calls without parts.
    """
    parts: list[types.Part] = []
    if result.text.strip():
        parts.append(types.Part(text=result.text))
    parts += result.call_parts or [types.Part(function_call=fc) for fc in result.function_calls]
    return types.Content(role="model", parts=parts)


async def _run_tools(
    calls: list[types.FunctionCall], ctx: registry.ToolContext, out: TurnResult
) -> list[types.Part]:
    results = await asyncio.gather(
        *(registry.dispatch(c.name or "", dict(c.args or {}), ctx) for c in calls)
    )
    parts: list[types.Part] = []
    for call, (payload, ms, succeeded) in zip(calls, results):
        out.tool_calls.append(
            {
                "name": call.name,
                "args": dict(call.args or {}),
                "ms": ms,
                "ok": succeeded,
                "error": payload.get("error") if not succeeded else None,
            }
        )
        if payload.get("pending_action_id"):
            out.proposed_action_id = payload["pending_action_id"]
        data = payload.get("data")
        if call.name in {"search_email", "get_calendar", "search_drive"} and isinstance(data, list):
            out.collection_results.append(
                {"name": call.name, "data": data, "args": dict(call.args or {})}
            )
        if call.name == "read_sheet" and isinstance(data, dict) and data.get("spreadsheet_url"):
            out.resource_links.append(
                {
                    "url": str(data["spreadsheet_url"]),
                    "markdown": f"[Open spreadsheet]({data['spreadsheet_url']})",
                }
            )
        if call.name == "render_visual" and isinstance(data, dict) and data.get("url"):
            out.visual_links.append(
                {
                    "url": str(data["url"]),
                    "markdown": str(
                        data.get("link_markdown") or f"[View interactive visual]({data['url']})"
                    ),
                }
            )
        parts.append(
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response=_serialisable(payload)
                )
            )
        )
    return parts


PLAIN_VISUAL_LINK = re.compile(
    r"(?im)^\s*view\s+(?:the\s+)?interactive\s+(?:visual|chart)\s*[.!]?\s*$"
)


def _ensure_visual_links(text: str, links: list[dict[str, str]]) -> str:
    """A successful visual tool must never degrade into an unlinked label in Telegram."""
    output = text.strip()
    for link in links:
        url = link["url"]
        markdown = link["markdown"]
        if url in output:
            continue
        replaced, count = PLAIN_VISUAL_LINK.subn(markdown, output, count=1)
        output = replaced if count else f"{output}\n\n{markdown}".strip()
    return output


def _ensure_resource_links(text: str, links: list[dict[str, str]]) -> str:
    output = text.strip()
    for link in links:
        if link["url"] not in output:
            output = f"{output}\n\n{link['markdown']}".strip()
    return output


DETAIL_TOOLS = {"get_email_detail", "read_drive_file"}
MIME_LABELS = {
    "application/vnd.google-apps.folder": "Folder",
    "application/vnd.google-apps.spreadsheet": "Spreadsheet",
    "application/vnd.google-apps.document": "Document",
    "application/vnd.google-apps.presentation": "Presentation",
    "application/pdf": "PDF",
}


def _repair_collection_dump(
    text: str,
    collections: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    user: User,
) -> str:
    """Replace only obvious single-paragraph list dumps; leave real analysis untouched."""
    if len(collections) != 1 or any(call.get("name") in DETAIL_TOOLS for call in tool_calls):
        return text
    collection = collections[0]
    rows = collection.get("data") or []
    name = collection.get("name")
    urls = _collection_urls(rows)
    missing_links = any(url not in text for url in urls)
    if (
        not rows
        or (len(rows) < 2 and not missing_links)
        or (_has_collection_layout(text, len(rows)) and not missing_links)
        or not _looks_like_collection_dump(text, name, rows)
    ):
        return text

    zone = _zone(user.timezone)
    if name == "search_email":
        return _format_email_list(rows, zone)
    if name == "get_calendar":
        return _format_calendar_list(rows, zone)
    if name == "search_drive":
        return _format_drive_list(rows, zone)
    return text


def _has_collection_layout(text: str, item_count: int) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= min(item_count + 1, 3):
        return True
    item_lines = sum(
        bool(re.match(r"^(?:[-•]|\d+[.)])\s+|^\*\*.+?\*\*\s*[—:-]", line))
        for line in lines
    )
    return item_lines >= min(item_count, 2)


def _looks_like_collection_dump(text: str, name: Any, rows: list[dict[str, Any]]) -> bool:
    """Distinguish an enumerated prose dump from a useful cross-record synthesis."""
    lowered = text.lower()
    noun = {"search_email": "emails", "get_calendar": "events", "search_drive": "files"}.get(name)
    if noun and re.search(rf"\b(?:{len(rows)}|few|several|multiple)\s+{noun}\b", lowered):
        return True

    matches = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if name == "search_email":
            candidates = (_sender_label(row.get("from")), row.get("subject"))
        elif name == "get_calendar":
            candidates = (row.get("summary"),)
        else:
            candidates = (row.get("name"),)
        if any(_marker_in_text(_safe_text(candidate, 60), lowered) for candidate in candidates):
            matches += 1
    return matches >= min(len(rows), 2)


def _marker_in_text(marker: str, lowered_text: str) -> bool:
    lowered_marker = marker.lower()
    if len(lowered_marker) >= 4 and lowered_marker in lowered_text:
        return True
    distinctive = [token for token in re.findall(r"[a-z0-9]+", lowered_marker) if len(token) >= 4]
    return any(re.search(rf"\b{re.escape(token)}\b", lowered_text) for token in distinctive)


def _format_email_list(rows: list[dict[str, Any]], zone: ZoneInfo) -> str:
    lines = [f"📬 **{len(rows)} emails found**"]
    for row in rows[:4]:
        sender = _sender_label(row.get("from"))
        subject = _safe_text(row.get("subject") or row.get("snippet") or "No subject", 92)
        subject = _google_resource_link(subject, row.get("web_url"))
        when = _date_time_label(row.get("date"), zone)
        suffix = f" · {when}" if when else ""
        lines.append(f"- **{sender}** — {subject}{suffix}")
    return _with_remainder(lines, len(rows))


def _google_resource_link(label: str, value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "google.com" or host.endswith(".google.com")):
        return label
    return f"[{label}]({url})"


def _format_calendar_list(rows: list[dict[str, Any]], zone: ZoneInfo) -> str:
    lines = [f"🗓 **{len(rows)} events found**"]
    for row in rows[:4]:
        start = row.get("start") or {}
        raw_start = start.get("dateTime") or start.get("date") if isinstance(start, dict) else start
        when = _date_time_label(raw_start, zone, all_day=isinstance(start, dict) and "date" in start)
        title = _safe_text(row.get("summary") or "Untitled event", 100)
        title = _google_resource_link(title, row.get("web_url"))
        lines.append(f"- **{when or 'Time unavailable'}** — {title}")
    return _with_remainder(lines, len(rows))


def _format_drive_list(rows: list[dict[str, Any]], zone: ZoneInfo) -> str:
    lines = [f"📁 **{len(rows)} files found**"]
    for row in rows[:4]:
        name = _safe_text(row.get("name") or "Untitled file", 92)
        linked_name = _google_resource_link(name, row.get("web_url") or row.get("webViewLink"))
        display_name = linked_name if linked_name != name else f"**{name}**"
        kind = MIME_LABELS.get(str(row.get("mimeType") or ""), "File")
        modified = _date_time_label(row.get("modifiedTime"), zone, date_only=True)
        suffix = f" · updated {modified}" if modified else ""
        lines.append(f"- {display_name} — {kind}{suffix}")
    return _with_remainder(lines, len(rows))


def _with_remainder(lines: list[str], item_count: int) -> str:
    if item_count > 4:
        lines.append(f"+ {item_count - 4} more matches")
    return "\n".join(lines)


def _collection_urls(rows: list[dict[str, Any]]) -> list[str]:
    return [
        str(row.get("web_url") or row.get("webViewLink") or "")
        for row in rows
        if isinstance(row, dict) and (row.get("web_url") or row.get("webViewLink"))
    ]


def _sender_label(value: Any) -> str:
    raw = str(value or "Unknown sender")
    name, address = parseaddr(raw)
    label = name.strip(' "') or address.split("@", 1)[0] or raw
    return _safe_text(label, 44)


def _safe_text(value: Any, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    clean = clean.translate(str.maketrans({"*": "", "_": "", "[": "", "]": ""}))
    if len(clean) <= limit:
        return clean
    clipped = clean[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:—- ")
    return f"{clipped}…"


def _date_time_label(
    value: Any,
    zone: ZoneInfo,
    *,
    all_day: bool = False,
    date_only: bool = False,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw) if "," in raw else datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
    except (TypeError, ValueError, OverflowError):
        return _safe_text(raw, 28)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(zone)
    date_label = f"{local.day} {local:%b}"
    if all_day:
        return f"{date_label} · all day"
    if date_only:
        return date_label
    hour = local.strftime("%I").lstrip("0") or "0"
    return f"{date_label} · {hour}:{local:%M %p}"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _serialisable(payload: dict[str, Any]) -> dict[str, Any]:
    """FunctionResponse.response must be a JSON-ish dict. ObjectIds and datetimes are not."""
    import json

    return json.loads(json.dumps(payload, default=str))


def _add_tokens(out: TurnResult, result: provider.Result) -> None:
    for key, value in (result.tokens or {}).items():
        out.tokens[key] = out.tokens.get(key, 0) + value
