"""Audit and daily-dedupe repository for proactive deliveries."""

from datetime import datetime
from typing import Any

from atlas.db.client import db
from atlas.db.models import utcnow


async def record(
    user_id: Any,
    kind: str,
    *,
    ref_id: Any = None,
    item_count: int = 0,
    candidates_considered: int = 0,
) -> None:
    await db().deliveries.insert_one(
        {
            "user_id": user_id,
            "kind": kind,
            "ref_id": ref_id,
            "item_count": item_count,
            "candidates_considered": candidates_considered,
            "sent_at": utcnow(),
        }
    )


async def exists_between(user_id: Any, kind: str, start: datetime, end: datetime) -> bool:
    query = {"user_id": user_id, "kind": kind, "sent_at": {"$gte": start, "$lt": end}}
    return await db().deliveries.find_one(query) is not None
