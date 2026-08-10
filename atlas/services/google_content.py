"""Bounded text extraction for Gmail messages and Google Drive files."""

import asyncio
import base64
import io
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from pypdf import PdfReader

from atlas.services.util import http

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
TEXT_MIME_TYPES = {"text/plain", "text/csv", "text/markdown", "application/json"}
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 60_000


async def gmail_get_message_full(token: str, message_id: str) -> dict[str, Any]:
    data = await _get_json(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(message_id, safe='')}",
        token,
        "Gmail message",
        params={"format": "full"},
    )
    if "error" in data:
        return data
    payload = data.get("payload", {})
    headers = {
        item.get("name", "").lower(): item.get("value", "")
        for item in payload.get("headers", [])
    }
    plain, html, attachments, body_refs = _gmail_parts(payload)
    if body_refs:
        bodies = await asyncio.gather(
            *(
                _get_json(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                    f"{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}",
                    token,
                    "Gmail message body",
                )
                for _, attachment_id in body_refs
            )
        )
        for (mime_type, _), body_data in zip(body_refs, bodies):
            if "error" in body_data or not body_data.get("data"):
                continue
            decoded = _decode_base64url(str(body_data["data"]))
            (plain if mime_type == "text/plain" else html).append(decoded)
    body = "\n\n".join(part for part in plain if part.strip()).strip()
    if not body and html:
        body = _html_text("\n".join(html))
    if not body:
        body = str(data.get("snippet") or "")
    body, truncated, char_count = _bounded(body)
    return {
        "id": message_id,
        "thread_id": data.get("threadId", ""),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "date": headers.get("date", ""),
        "body": body,
        "body_truncated": truncated,
        "body_char_count": char_count,
        "attachments": attachments,
    }


async def drive_read_file(
    token: str,
    file_id: str,
    mime_type: str = "",
    name: str = "",
) -> dict[str, Any]:
    encoded_id = quote(file_id, safe="")
    if not mime_type or not name:
        metadata = await _get_json(
            f"https://www.googleapis.com/drive/v3/files/{encoded_id}",
            token,
            "Google Drive metadata",
            params={"fields": "id,name,mimeType,size,modifiedTime"},
        )
        if "error" in metadata:
            return metadata
        mime_type = mime_type or str(metadata.get("mimeType") or "")
        name = name or str(metadata.get("name") or "")

    export_mime = {
        GOOGLE_DOC: "text/plain",
        GOOGLE_SHEET: "text/csv",
        GOOGLE_SLIDES: PDF,
    }.get(mime_type)
    if export_mime:
        url = f"https://www.googleapis.com/drive/v3/files/{encoded_id}/export"
        params = {"mimeType": export_mime}
        content_mime = export_mime
    elif mime_type == PDF or mime_type in TEXT_MIME_TYPES or mime_type.startswith("text/"):
        url = f"https://www.googleapis.com/drive/v3/files/{encoded_id}"
        params = {"alt": "media"}
        content_mime = mime_type
    else:
        return {
            "error": (
                f"Drive file type '{mime_type or 'unknown'}' is not readable yet. "
                "Use a Google Doc, Sheet, Slides file, PDF, or text file."
            )
        }

    raw = await _get_bytes(url, token, "Google Drive file", params=params)
    if isinstance(raw, dict):
        return raw
    if len(raw) > MAX_FILE_BYTES:
        return {"error": "Google Drive file is larger than the 15MB reading limit."}

    page_count = 0
    if content_mime == PDF:
        try:
            page_count, text = await asyncio.to_thread(_pdf_text, raw)
        except Exception as exc:
            return {"error": f"Google Drive PDF could not be parsed ({type(exc).__name__})"}
    else:
        text = raw.decode("utf-8", errors="replace")

    text, truncated, char_count = _bounded(text)
    return {
        "file_id": file_id,
        "name": name,
        "mime_type": mime_type,
        "text": text,
        "text_truncated": truncated,
        "text_char_count": char_count,
        "page_count": page_count,
    }


def _gmail_parts(
    part: dict[str, Any],
) -> tuple[list[str], list[str], list[dict[str, str]], list[tuple[str, str]]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict[str, str]] = []
    body_refs: list[tuple[str, str]] = []
    filename = str(part.get("filename") or "")
    body = part.get("body", {})
    mime_type = str(part.get("mimeType") or "")
    if filename:
        attachments.append(
            {
                "filename": filename,
                "mime_type": mime_type,
                "attachment_id": str(body.get("attachmentId") or ""),
            }
        )
    elif body.get("data"):
        decoded = _decode_base64url(str(body["data"]))
        if mime_type == "text/plain":
            plain.append(decoded)
        elif mime_type == "text/html":
            html.append(decoded)
    elif body.get("attachmentId") and mime_type in {"text/plain", "text/html"}:
        body_refs.append((mime_type, str(body["attachmentId"])))
    for child in part.get("parts", []) or []:
        child_plain, child_html, child_attachments, child_refs = _gmail_parts(child)
        plain.extend(child_plain)
        html.extend(child_html)
        attachments.extend(child_attachments)
        body_refs.extend(child_refs)
    return plain, html, attachments, body_refs


def _decode_base64url(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _html_text(value: str) -> str:
    parser = _TextParser()
    parser.feed(value)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parser.parts)).strip()


def _bounded(value: str) -> tuple[str, bool, int]:
    clean = value.replace("\x00", "").strip()
    count = len(clean)
    return clean[:MAX_TEXT_CHARS], count > MAX_TEXT_CHARS, count


def _pdf_text(raw: bytes) -> tuple[int, str]:
    reader = PdfReader(io.BytesIO(raw))
    return len(reader.pages), "\n\n".join(page.extract_text() or "" for page in reader.pages)


async def _get_json(
    url: str, token: str, what: str, params: Any = None
) -> dict[str, Any]:
    for attempt in range(2):
        try:
            response = await http().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            if attempt:
                return {"error": f"{what} unavailable ({type(exc).__name__})"}
            await asyncio.sleep(0.35)
    return {"error": f"{what} unavailable"}


async def _get_bytes(
    url: str, token: str, what: str, params: Any = None
) -> bytes | dict[str, Any]:
    for attempt in range(2):
        try:
            response = await http().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=25,
            )
            response.raise_for_status()
            return bytes(response.content)
        except Exception as exc:
            if attempt:
                return {"error": f"{what} unavailable ({type(exc).__name__})"}
            await asyncio.sleep(0.35)
    return {"error": f"{what} unavailable"}
