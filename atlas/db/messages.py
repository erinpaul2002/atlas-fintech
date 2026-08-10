"""messages — conversation history. Feeds the context window and the recall layer."""

from typing import Any

from atlas.db.client import db
from atlas.db.models import Message, utcnow


async def append(
    user_id: Any,
    role: str,
    content: str,
    tool_name: str | None = None,
    media_kind: str | None = None,
    media_ref: str | None = None,
) -> Any:
    res = await db().messages.insert_one(
        {
            "user_id": user_id,
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "media_kind": media_kind,
            "media_ref": media_ref,
            "embedding": None,
            "created_at": utcnow(),
        }
    )
    return res.inserted_id


async def recent(user_id: Any, limit: int = 30) -> list[Message]:
    """Last N user/assistant turns, oldest first. Tool results are not replayed — they're stale
    by the next turn and the model would quote them as live figures."""
    cursor = (
        db().messages.find({"user_id": user_id, "role": {"$in": ["user", "assistant"]}})
        .sort("created_at", -1)
        .limit(limit)
    )
    docs = [Message(**d) async for d in cursor]
    return list(reversed(docs))


async def has_history(user_id: Any) -> bool:
    return await db().messages.count_documents({"user_id": user_id}, limit=1) > 0
