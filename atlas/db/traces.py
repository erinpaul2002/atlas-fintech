"""traces — one document per turn. The only way to catch a write that fired without a confirm."""

import logging
from typing import Any

from atlas.db.client import db
from atlas.db.models import utcnow

log = logging.getLogger(__name__)


async def write(
    user_id: Any,
    message_id: Any,
    rounds: int,
    tool_calls: list[dict[str, Any]],
    total_ms: int,
    provider: str,
    reply: str,
    action: dict[str, Any] | None = None,
    tokens: dict[str, int] | None = None,
) -> None:
    try:
        await db().traces.insert_one(
            {
                "user_id": user_id,
                "message_id": message_id,
                "rounds": rounds,
                "tool_calls": tool_calls,
                "action": action or {"proposed": False, "confirmed": False, "executed": False},
                "tokens": tokens or {},
                "total_ms": total_ms,
                "provider": provider,
                "reply": reply,
                "created_at": utcnow(),
            }
        )
    except Exception as exc:  # observability must never break a turn
        log.warning("trace write failed: %s", exc)
