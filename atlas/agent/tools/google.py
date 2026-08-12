"""Composite Google tools. Tokens stay below this boundary and never reach the model."""

from typing import Any

from atlas.agent import actions
from atlas.agent.tools.google_auth import connection_link, not_connected
from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.db import integrations
from atlas.services import google
from atlas.services.util import now_iso


@tool(
    "connect_google",
    "Create a one-time link that connects Google Sheets, Gmail, Calendar and Drive. Use only when asked or when a Google tool says not_connected.",
    {"type": "object", "properties": {}},
)
async def connect_google(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return ok({"link": await connection_link(ctx)}, source="Atlas Google OAuth")


@tool(
    "read_sheet",
    "Read and analyze a Google Sheet. Public link-shared sheets work without connecting Google; private sheets use OAuth automatically.",
    {
        "type": "object",
        "properties": {
            "url_or_id": {"type": "string", "description": "Google Sheets URL or spreadsheet id"},
            "range": {"type": "string", "description": "Optional A1 range, such as Revenue!A1:F50"},
        },
        "required": ["url_or_id"],
    },
)
async def read_sheet(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    parsed = google.parse_spreadsheet(str(args.get("url_or_id") or ""))
    if not parsed:
        return {"error": "That does not look like a Google Sheets link or id."}
    spreadsheet_id, gid = parsed
    range_ = str(args.get("range") or "").strip() or None

    rows = await google.public_sheet(spreadsheet_id, gid=gid, range_=range_)
    if isinstance(rows, list):
        data = {**google.sheet_result(rows), "spreadsheet_url": google.spreadsheet_url(spreadsheet_id, gid)}
        return ok(data, source="Google Sheets (public link)", as_of=now_iso())

    token = await integrations.google_access_token(ctx.user.id, integrations.SPREADSHEETS_SCOPE)
    if not token:
        return await not_connected(ctx)
    rows = await google.sheet_values(token, spreadsheet_id, range_ or "A:Z")
    if not isinstance(rows, list):
        return rows
    data = {**google.sheet_result(rows), "spreadsheet_url": google.spreadsheet_url(spreadsheet_id, gid)}
    return ok(data, source="Google Sheets", as_of=now_iso())


@tool(
    "propose_sheet_write",
    "Propose, but never execute, a Google Sheets write. After this tool, state exactly what will change and stop for confirmation.",
    {
        "type": "object",
        "properties": {
            "url_or_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["append", "new_tab", "update_cells"]},
            "target": {"type": "string", "description": "Tab name, or an A1 range for update_cells"},
            "values": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "Rows to write; numbers may be strings because Sheets parses them"},
            "note": {"type": "string"},
        },
        "required": ["url_or_id", "mode", "target", "values"],
    },
)
async def propose_sheet_write(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.SPREADSHEETS_SCOPE)
    if not token:
        return await not_connected(ctx)
    parsed = google.parse_spreadsheet(str(args.get("url_or_id") or ""))
    if not parsed:
        return {"error": "That does not look like a Google Sheets link or id."}
    mode = str(args.get("mode") or "")
    if mode not in {"append", "new_tab", "update_cells"}:
        return {"error": "Unsupported sheet write mode."}
    values = args.get("values") or []
    if not isinstance(values, list) or not values or any(not isinstance(row, list) for row in values):
        return {"error": "The sheet write needs at least one row of values."}
    if len(values) > 2000 or any(len(row) > 100 for row in values):
        return {"error": "That write is too large for one action; narrow it to 2,000 rows and 100 columns."}

    target = str(args.get("target") or "Sheet1").strip()
    titles = await google.spreadsheet_titles(token, parsed[0])
    if isinstance(titles, dict) and "error" in titles:
        return titles
    target_tab = target.split("!", 1)[0].strip("'")
    if mode == "new_tab" and target in titles:
        return {"error": f"A tab named '{target}' already exists; choose a new name so nothing is overwritten."}
    if mode == "new_tab" and (len(target) > 100 or any(char in target for char in "[]*?/\\:")):
        return {"error": "That tab name is not valid in Google Sheets."}
    if mode == "append" and target_tab not in titles:
        return {"error": f"There is no tab named '{target_tab}' in that spreadsheet."}
    if mode == "update_cells":
        if actions.target_start(target)[1] is None:
            return {"error": "update_cells needs an A1 target such as Tracker!B4."}
        if "!" in target and target_tab not in titles:
            return {"error": f"There is no tab named '{target_tab}' in that spreadsheet."}

    spec = {
        "spreadsheet_id": parsed[0],
        "mode": mode,
        "target": target,
        "values": values,
        "note": str(args.get("note") or "").strip(),
        "destructive": mode == "update_cells",
    }
    pending = await actions.propose(ctx.user.id, spec)
    data = {"summary": pending.summary, "status": pending.status, "row_count": len(values)}
    extra = {"pending_action_id": str(pending.id)} if pending.status == "pending" else {}
    return ok(data, source="pending action", **extra)


@tool(
    "search_email",
    "Search the connected Gmail inbox for company context, threads or messages the user asks about.",
    {
        "type": "object",
        "properties": {"query": {"type": "string"}, "max": {"type": "integer"}},
        "required": ["query"],
    },
)
async def search_email(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.GMAIL_SCOPE)
    if not token:
        return await not_connected(ctx)
    result = await google.gmail_search(token, str(args.get("query") or ""), int(args.get("max") or 10))
    return result if isinstance(result, dict) and "error" in result else ok(
        result,
        source="Gmail",
        as_of=now_iso(),
        presentation="Lead with the match count; show one email per line as sender — linked subject · time using web_url.",
    )


@tool(
    "get_calendar",
    "Read events and attendees from the connected Google Calendar over an explicit time range.",
    {
        "type": "object",
        "properties": {"from": {"type": "string", "description": "RFC3339 start"}, "to": {"type": "string", "description": "RFC3339 end"}},
        "required": ["from", "to"],
    },
)
async def get_calendar(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.CALENDAR_READ_SCOPE)
    if not token:
        return await not_connected(ctx)
    result = await google.calendar_events(token, str(args.get("from") or ""), str(args.get("to") or ""))
    return result if isinstance(result, dict) and "error" in result else ok(
        result,
        source="Google Calendar",
        as_of=now_iso(),
        presentation="Lead with the event count; show one event per line as time — linked title using web_url.",
    )


@tool(
    "search_drive",
    "Find or list files and folders in the connected Google Drive. For requests like 'any folders' or 'what sheets can you see', use an empty query and the matching kind.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional name or content search; leave empty to list"},
            "kind": {
                "type": "string",
                "enum": ["any", "folder", "spreadsheet", "document", "pdf"],
                "description": "Limit results to a Drive item type",
            },
            "max": {"type": "integer"},
        },
    },
)
async def search_drive(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.DRIVE_SCOPE)
    if not token:
        return await not_connected(ctx)
    result = await google.drive_search(
        token,
        str(args.get("query") or ""),
        int(args.get("max") or 10),
        str(args.get("kind") or "any"),
    )
    return result if isinstance(result, dict) and "error" in result else ok(
        result,
        source="Google Drive",
        as_of=now_iso(),
        presentation="Lead with the match count; show one file per line as linked name using web_url — type · modified date.",
    )
