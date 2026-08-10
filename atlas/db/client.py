"""Mongo client + idempotent index creation at startup. No migration step (DATA_MODEL.md)."""

import logging

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from atlas.config import settings

log = logging.getLogger(__name__)

_client: AsyncMongoClient | None = None


def db() -> AsyncDatabase:
    global _client
    if _client is None:
        _client = AsyncMongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=8000, tz_aware=True)
    return _client[settings.mongodb_db]


async def ping() -> bool:
    await db().command("ping")
    return True


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


# (collection, keys, kwargs)
INDEXES = [
    ("users", [("telegram_chat_id", 1)], {"unique": True}),
    ("users", [("brief_hour_local", 1), ("brief_minute_local", 1)], {}),
    ("users", [("watchlist.symbol", 1)], {}),
    ("messages", [("user_id", 1), ("created_at", -1)], {}),
    ("facts", [("user_id", 1), ("superseded_by", 1), ("created_at", -1)], {}),
    ("alerts", [("active", 1), ("kind", 1)], {}),
    ("documents", [("user_id", 1), ("created_at", -1)], {}),
    ("pending_actions", [("user_id", 1), ("status", 1), ("proposed_at", -1)], {}),
    ("pending_actions", [("idempotency_key", 1)], {"unique": True}),
    # only pending docs carry proposed_at; status moves past pending unset it, so completed
    # actions survive as an audit trail (DATA_MODEL.md § pending_actions)
    ("pending_actions", [("proposed_at", 1)], {"expireAfterSeconds": 600}),
    ("traces", [("user_id", 1), ("created_at", -1)], {}),
    ("traces", [("created_at", 1)], {"expireAfterSeconds": 604800}),
    ("integrations", [("user_id", 1), ("provider", 1)], {"unique": True}),
    ("oauth_states", [("created_at", 1)], {"expireAfterSeconds": 900}),
    ("seen_items", [("user_id", 1), ("item_key", 1)], {"unique": True}),
    ("seen_items", [("first_seen_at", 1)], {"expireAfterSeconds": 2592000}),
    ("deliveries", [("user_id", 1), ("kind", 1), ("sent_at", -1)], {}),
]

VECTOR_INDEXES = [
    ("documents", "digest_embedding"),
    ("messages", "embedding"),
]

EMBED_DIMS = 768


async def ensure_indexes() -> None:
    database = db()
    for coll, keys, kwargs in INDEXES:
        try:
            await database[coll].create_index(keys, **kwargs)
        except Exception as exc:  # an index that already exists with other options must not kill boot
            log.warning("index %s %s failed: %s", coll, keys, exc)

    for coll, field in VECTOR_INDEXES:
        model = {
            "name": f"vec_{field}",
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {"type": "vector", "path": field, "numDimensions": EMBED_DIMS, "similarity": "cosine"},
                    {"type": "filter", "path": "user_id"},
                ]
            },
        }
        try:
            await database[coll].create_search_index(model)
            log.info("created vector index %s.%s", coll, field)
        except Exception as exc:
            # already exists, or the tier/driver doesn't allow programmatic creation — not fatal,
            # vector recall degrades to "no results" and everything else keeps working
            log.info("vector index %s.%s not created: %s", coll, field, str(exc)[:200])
