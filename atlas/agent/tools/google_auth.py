"""Shared OAuth reconnection helpers for Google agent tools."""

from typing import Any
from urllib.parse import urlencode

from atlas.config import settings
from atlas.db import integrations
from atlas.db import users as users_repo


async def connection_link(ctx: Any) -> str:
    state = await integrations.create_oauth_state(ctx.user.id)
    await users_repo.patch(ctx.user.id, {"google_offer_shown": True})
    query = urlencode({"state": state})
    return f"{settings.effective_public_base_url}/oauth/google/start?{query}"


async def not_connected(ctx: Any) -> dict[str, Any]:
    return {"error": "not_connected", "link": await connection_link(ctx)}
