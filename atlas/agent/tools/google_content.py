"""Read full Gmail messages and content from files found in Google Drive."""

from typing import Any

from atlas.agent.tools.google_auth import not_connected
from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.db import integrations
from atlas.services import google
from atlas.services.util import now_iso


@tool(
    "get_email_detail",
    "Retrieve the full body and headers of one Gmail message returned by search_email. Use when a snippet is insufficient for detailed summarization.",
    {
        "type": "object",
        "properties": {"message_id": {"type": "string"}},
        "required": ["message_id"],
    },
)
async def get_email_detail(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.GMAIL_SCOPE)
    if not token:
        return await not_connected(ctx)
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        return {"error": "Gmail message id is required."}
    result = await google.gmail_get_message_full(token, message_id)
    return result if "error" in result else ok(result, source="Gmail", as_of=now_iso())


@tool(
    "read_drive_file",
    "Read text from a Google Drive file returned by search_drive. Supports Google Docs, Sheets, Slides, PDFs, and text files.",
    {
        "type": "object",
        "properties": {
            "file_id": {"type": "string"},
            "mime_type": {
                "type": "string",
                "description": "mimeType from search_drive; omit to fetch metadata",
            },
            "name": {"type": "string", "description": "File name from search_drive"},
        },
        "required": ["file_id"],
    },
)
async def read_drive_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    token = await integrations.google_access_token(ctx.user.id, integrations.DRIVE_SCOPE)
    if not token:
        return await not_connected(ctx)
    file_id = str(args.get("file_id") or "").strip()
    if not file_id:
        return {"error": "Google Drive file id is required."}
    result = await google.drive_read_file(
        token,
        file_id,
        str(args.get("mime_type") or "").strip(),
        str(args.get("name") or "").strip(),
    )
    return result if "error" in result else ok(result, source="Google Drive", as_of=now_iso())
