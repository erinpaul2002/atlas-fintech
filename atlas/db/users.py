"""users — one document per Telegram chat. Read on every turn."""

from typing import Any

from pymongo import ReturnDocument

from atlas.db.client import db
from atlas.db.models import User, WatchlistItem, utcnow


async def get_or_create(chat_id: int, telegram_user_id: int, first_name: str) -> User:
    doc = await db().users.find_one_and_update(
        {"telegram_chat_id": chat_id},
        {
            "$setOnInsert": {
                "telegram_user_id": telegram_user_id,
                "first_name": first_name,
                "created_at": utcnow(),
            },
            "$set": {"updated_at": utcnow()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return User(**doc)


async def by_id(user_id: Any) -> User | None:
    doc = await db().users.find_one({"_id": user_id})
    return User(**doc) if doc else None


async def authorize_user(user_id: Any, telegram_user_id: int) -> User | None:
    """Persist a passcode grant for one Telegram sender within this chat."""
    doc = await db().users.find_one_and_update(
        {"_id": user_id},
        {
            "$addToSet": {"authorized_telegram_user_ids": telegram_user_id},
            "$set": {"is_authorized": True, "updated_at": utcnow()},
        },
        return_document=ReturnDocument.AFTER,
    )
    return User(**doc) if doc else None


ALLOWED_PATCH = {
    "first_name", "role", "timezone", "brief_hour_local", "brief_minute_local",
    "interests", "intel_preferences", "preferences", "onboarding_offered", "google_offer_shown",
}

ONBOARDING_STATUSES = {"new", "in_progress", "completed", "skipped"}


async def patch(user_id: Any, fields: dict[str, Any]) -> User | None:
    """Whitelisted so a model-supplied patch can't rewrite the watchlist or the narrative."""
    clean = {k: v for k, v in fields.items() if k in ALLOWED_PATCH and v is not None}
    if not clean:
        return await by_id(user_id)
    clean["updated_at"] = utcnow()
    doc = await db().users.find_one_and_update(
        {"_id": user_id}, {"$set": clean}, return_document=ReturnDocument.AFTER
    )
    return User(**doc) if doc else None


async def set_narrative(user_id: Any, narrative: str) -> None:
    await db().users.update_one(
        {"_id": user_id},
        {"$set": {"profile_narrative": narrative, "profile_narrative_at": utcnow()}},
    )


async def mark_onboarding_offered(user_id: Any, gaps: list[str]) -> None:
    if not gaps:
        return
    await db().users.update_one(
        {"_id": user_id},
        {"$addToSet": {"onboarding_offered": {"$each": gaps}}, "$set": {"updated_at": utcnow()}},
    )


async def set_onboarding(user_id: Any, status: str, step: str) -> None:
    if status not in ONBOARDING_STATUSES:
        raise ValueError(f"unsupported onboarding status: {status}")
    await db().users.update_one(
        {"_id": user_id},
        {"$set": {"onboarding_status": status, "onboarding_step": step, "updated_at": utcnow()}},
    )


async def add_to_watchlist(user_id: Any, symbol: str, company_name: str, reason: str) -> User | None:
    symbol = symbol.upper().strip()
    await db().users.update_one({"_id": user_id}, {"$pull": {"watchlist": {"symbol": symbol}}})
    item = WatchlistItem(symbol=symbol, company_name=company_name, reason=reason)
    doc = await db().users.find_one_and_update(
        {"_id": user_id},
        {"$push": {"watchlist": item.model_dump()}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    return User(**doc) if doc else None


async def remove_from_watchlist(user_id: Any, symbol: str) -> User | None:
    doc = await db().users.find_one_and_update(
        {"_id": user_id},
        {"$pull": {"watchlist": {"symbol": symbol.upper().strip()}}, "$set": {"updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    return User(**doc) if doc else None


async def watchers_of(symbol: str) -> list[User]:
    cursor = db().users.find({"watchlist.symbol": symbol.upper().strip()})
    return [User(**d) async for d in cursor]


async def with_brief_due(hour: int, minute: int) -> list[User]:
    cursor = db().users.find({"brief_hour_local": hour, "brief_minute_local": minute})
    return [User(**d) async for d in cursor]


async def with_briefs_enabled() -> list[User]:
    cursor = db().users.find({"brief_hour_local": {"$ne": None}})
    return [User(**d) async for d in cursor]
