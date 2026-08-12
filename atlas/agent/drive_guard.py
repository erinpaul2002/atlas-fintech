"""Deterministic safety guard for requests that require Google Drive data."""

import re
from typing import Any


DRIVE_INTENT = re.compile(
    r"\b(?:google\s+drive|my\s+drive|in\s+(?:my\s+)?drive|on\s+(?:my\s+)?drive|"
    r"from\s+(?:my\s+)?drive|drive\s+(?:files?|folders?|documents?|docs?|sheets?|"
    r"spreadsheets?|pdfs?|images?|photos?)|"
    r"(?:files?|folders?|documents?|docs?|sheets?|spreadsheets?|pdfs?|images?|photos?)"
    r"\s+(?:named|called))\b",
    re.IGNORECASE,
)
LIST_INTENT = re.compile(r"\b(?:list|show|what|which|any)\b", re.IGNORECASE)
TRAILING_DRIVE = re.compile(r"\s+(?:in|on|from)\s+(?:my\s+|google\s+)?drive\b.*$", re.IGNORECASE)
QUERY_PATTERNS = (
    re.compile(r"\b(?:named|called)\s+[\"'“”]?(.+?)[\"'“”]?(?:[?.!]|$)", re.IGNORECASE),
    re.compile(
        r"\b(?:images?|photos?|files?|documents?|docs?|pdfs?|sheets?|spreadsheets?|folders?)"
        r"\s+(?:of|for)\s+(?:my\s+)?(.+?)(?:[?.!]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:find|look\s+for|search\s+for)\s+(?:an?\s+|the\s+)?"
        r"(?:(?:images?|photos?|files?|documents?|docs?|pdfs?|sheets?|spreadsheets?|folders?)\s+)?"
        r"(?:of\s+|named\s+|called\s+)?(?:my\s+)?(.+?)(?:[?.!]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdrive\s+for\s+(?:an?\s+|the\s+)?"
        r"(?:(?:images?|photos?|files?|documents?|docs?|pdfs?|sheets?|spreadsheets?|folders?)\s+)?"
        r"(.+?)(?:[?.!]|$)",
        re.IGNORECASE,
    ),
)


def required_drive_args(turn: Any) -> dict[str, Any] | None:
    """Return search args only when the current turn clearly refers to Google Drive."""
    text = _turn_text(turn)
    if not DRIVE_INTENT.search(text):
        return None

    kind = _kind(text)
    query = _query(text)
    return {
        "query": query,
        "kind": kind,
        "max": 25 if not query else 10,
        "include_content": _content_search(text),
        "exact_name": bool(re.search(r"\b(?:named|called)\b", text, re.IGNORECASE)),
    }


def _turn_text(turn: Any) -> str:
    return " ".join(
        str(part.text).strip()
        for part in (getattr(turn, "parts", None) or [])
        if getattr(part, "text", None)
    ).strip()


def _kind(text: str) -> str:
    if re.search(r"\bfolders?\b", text, re.IGNORECASE):
        return "folder"
    if re.search(r"\b(?:spreadsheets?|sheets?)\b", text, re.IGNORECASE):
        return "spreadsheet"
    if re.search(r"\b(?:documents?|docs?)\b", text, re.IGNORECASE):
        return "document"
    if re.search(r"\bpdfs?\b", text, re.IGNORECASE):
        return "pdf"
    if re.search(r"\b(?:images?|photos?|pictures?)\b", text, re.IGNORECASE):
        return "image"
    return "any"


def _query(text: str) -> str:
    # A plain listing should not accidentally search for the words after "Drive".
    if LIST_INTENT.search(text) and not re.search(
        r"\b(?:named|called|find|look\s+for|search\s+for|drive\s+for)\b", text, re.IGNORECASE
    ):
        return ""
    for pattern in QUERY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        query = TRAILING_DRIVE.sub("", match.group(1)).strip(" \t\r\n\"'“”")
        query = re.sub(r"^(?:a|an|the)\s+", "", query, flags=re.IGNORECASE)
        query = re.sub(
            r"^(?:mention(?:s|ing|ed)?|contain(?:s|ing|ed)?)\s+", "", query,
            flags=re.IGNORECASE,
        )
        if query and query.lower() not in {"file", "files", "folder", "folders", "drive"}:
            return query[:160]
    return ""


def _content_search(text: str) -> bool:
    return bool(re.search(
        r"\b(?:mention(?:s|ing|ed)?|contain(?:s|ing|ed)?|says?\s+about|text\s+about|"
        r"content\s+about|inside)\b",
        text,
        re.IGNORECASE,
    ))
