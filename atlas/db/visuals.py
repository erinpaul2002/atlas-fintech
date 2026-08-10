"""Short-lived, shareable visual documents."""

import secrets
from typing import Any

from pymongo.errors import DuplicateKeyError

from atlas.db.client import db
from atlas.db.models import utcnow

MAX_HTML_BYTES = 512_000


async def save_visual(
    user_id: Any,
    title: str,
    kind: str,
    html_content: str,
    content_security_policy: str,
) -> str:
    """Store trusted renderer output and return an unguessable public identifier."""
    if len(html_content.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("visual output is too large")

    for _ in range(3):
        visual_id = f"v_{secrets.token_urlsafe(12)}"
        try:
            await db().visuals.insert_one(
                {
                    "_id": visual_id,
                    "visual_id": visual_id,
                    "user_id": user_id,
                    "title": title,
                    "kind": kind,
                    "html_content": html_content,
                    "content_security_policy": content_security_policy,
                    "created_at": utcnow(),
                    "views": 0,
                }
            )
            return visual_id
        except DuplicateKeyError:
            continue
    raise RuntimeError("could not allocate a visual id")


async def get_visual(visual_id: str) -> dict[str, Any] | None:
    document = await db().visuals.find_one({"_id": visual_id})
    if document is not None:
        await db().visuals.update_one({"_id": visual_id}, {"$inc": {"views": 1}})
    return document
