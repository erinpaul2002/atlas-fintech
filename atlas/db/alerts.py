"""alerts — every background watch the user created. `natural_language` is their exact words."""

from typing import Any

from atlas.db.client import db
from atlas.db.models import Alert, utcnow

KINDS = {"price_move", "filing", "news", "earnings", "time"}


async def create(
    user_id: Any, kind: str, symbol: str | None, params: dict[str, Any], natural_language: str
) -> Alert:
    doc = {
        "user_id": user_id,
        "kind": kind if kind in KINDS else "news",
        "symbol": symbol.upper().strip() if symbol else None,
        "params": params or {},
        "natural_language": natural_language.strip(),
        "active": True,
        "last_fired_at": None,
        "created_at": utcnow(),
    }
    res = await db().alerts.insert_one(doc)
    return Alert(**{**doc, "_id": res.inserted_id})


async def for_user(user_id: Any, active_only: bool = True) -> list[Alert]:
    query: dict[str, Any] = {"user_id": user_id}
    if active_only:
        query["active"] = True
    cursor = db().alerts.find(query).sort("created_at", -1)
    return [Alert(**d) async for d in cursor]


async def cancel(user_id: Any, alert_id: Any = None, symbol: str | None = None) -> int:
    query: dict[str, Any] = {"user_id": user_id, "active": True}
    if alert_id is not None:
        query["_id"] = alert_id
    if symbol:
        query["symbol"] = symbol.upper().strip()
    res = await db().alerts.update_many(query, {"$set": {"active": False}})
    return res.modified_count


async def due(kind: str) -> list[Alert]:
    cursor = db().alerts.find({"active": True, "kind": kind})
    return [Alert(**d) async for d in cursor]


async def mark_fired(alert_id: Any) -> None:
    await db().alerts.update_one({"_id": alert_id}, {"$set": {"last_fired_at": utcnow()}})
