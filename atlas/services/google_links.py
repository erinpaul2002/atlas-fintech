"""Stable user-facing links derived from Google API resource identifiers."""

from typing import Any
from urllib.parse import quote


def gmail_thread_url(thread_id: Any) -> str:
    """Open an API thread id in Gmail's All Mail view for the primary browser account."""
    value = str(thread_id or "").strip()
    if not value:
        return ""
    return f"https://mail.google.com/mail/u/0/#all/{quote(value, safe='')}"
