"""Per-user proactive-item deduplication."""

from typing import Any

from atlas.db.client import db
from atlas.db.models import utcnow


async def contains(user_id: Any, item_key: str) -> bool:
    return await db().seen_items.find_one({"user_id": user_id, "item_key": item_key}) is not None


async def mark(user_id: Any, item_key: str) -> None:
    await db().seen_items.update_one(
        {"user_id": user_id, "item_key": item_key},
        {"$setOnInsert": {"first_seen_at": utcnow()}},
        upsert=True,
    )
