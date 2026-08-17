"""Google Drive search helpers kept separate from the shared Google REST client."""

import re
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable


GetJson = Callable[..., Awaitable[dict[str, Any]]]


async def fuzzy_drive_files(
    get_json: GetJson,
    token: str,
    query: str,
    maximum: int,
    mime_clause: str | None,
    fields: str,
) -> Any:
    """Fallback for obvious filename misspellings after Drive's token search misses."""
    clauses = ["trashed = false"]
    if mime_clause:
        clauses.append(mime_clause)
    data = await get_json(
        "https://www.googleapis.com/drive/v3/files", token, "Google Drive fuzzy search",
        params={
            "q": " and ".join(clauses), "pageSize": 1000, "orderBy": "modifiedTime desc",
            "fields": fields,
        },
    )
    if "error" in data:
        return data
    needle = drive_name_key(query)
    ranked = sorted(
        (
            (SequenceMatcher(None, needle, drive_name_key(str(item.get("name") or ""))).ratio(), item)
            for item in data.get("files", [])
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [item for score, item in ranked if score >= 0.78][:max(1, min(maximum, 50))]


def drive_name_key(value: str) -> str:
    clean = re.sub(r"\.[a-z0-9]{1,8}$", "", value.strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", clean).strip()
