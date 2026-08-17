"""facts — the memory. Source of truth behind users.profile_narrative.

We never update a fact; a contradiction inserts a new one and supersedes the old, because
"you said last week you were bearish on this" is the line that makes the product feel alive.
"""

from typing import Any

from atlas.db.client import db
from atlas.db.models import Fact, utcnow

KINDS = {"interest", "position", "preference", "context", "person"}


async def add(
    user_id: Any,
    text: str,
    kind: str = "context",
    source_message_id: Any = None,
    confidence: float = 1.0,
    supersedes: Any = None,
) -> Any:
    res = await db().facts.insert_one(
        {
            "user_id": user_id,
            "kind": kind if kind in KINDS else "context",
            "text": text.strip(),
            "source_message_id": source_message_id,
            "confidence": confidence,
            "superseded_by": None,
            "created_at": utcnow(),
        }
    )
    if supersedes is not None:
        await db().facts.update_one(
            {"_id": supersedes, "user_id": user_id}, {"$set": {"superseded_by": res.inserted_id}}
        )
    # the narrative is a cache of these; a write makes it stale
    await db().users.update_one({"_id": user_id}, {"$set": {"profile_narrative_at": None}})
    return res.inserted_id


async def current(user_id: Any, limit: int = 200) -> list[Fact]:
    """All live facts, newest first. Dozens per user — they load wholesale, no top-k."""
    cursor = (
        db().facts.find({"user_id": user_id, "superseded_by": None})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [Fact(**d) async for d in cursor]


async def narrative_is_stale(user_id: Any) -> bool:
    doc = await db().users.find_one({"_id": user_id}, {"profile_narrative_at": 1, "profile_narrative": 1})
    if not doc:
        return False
    return not doc.get("profile_narrative") or doc.get("profile_narrative_at") is None
