"""Encrypted Google OAuth connections and short-lived CSRF state."""

import logging
import secrets
from datetime import timedelta
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from atlas.config import settings
from atlas.db.client import db
from atlas.db.models import Integration, utcnow
from atlas.services.util import http

log = logging.getLogger(__name__)

SPREADSHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
REQUESTED_SCOPES = [SPREADSHEETS_SCOPE, GMAIL_SCOPE, CALENDAR_SCOPE, DRIVE_SCOPE]
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _fernet() -> Fernet:
    return Fernet(settings.token_encryption_key.encode())


def _encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def _decrypt(value: bytes | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(bytes(value)).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


async def create_oauth_state(user_id: Any) -> str:
    state = secrets.token_urlsafe(32)
    await db().oauth_states.insert_one({"_id": state, "user_id": user_id, "created_at": utcnow()})
    return state


async def state_exists(state: str) -> bool:
    return await db().oauth_states.find_one({"_id": state}, {"_id": 1}) is not None


async def consume_oauth_state(state: str) -> Any | None:
    doc = await db().oauth_states.find_one_and_delete({"_id": state})
    return doc.get("user_id") if doc else None


async def save_google(user_id: Any, token: dict[str, Any]) -> Integration:
    expires_at = utcnow() + timedelta(seconds=max(60, int(token.get("expires_in") or 3600)))
    scopes = str(token.get("scope") or "").split() or REQUESTED_SCOPES
    fields: dict[str, Any] = {
        "provider": "google",
        "scopes": scopes,
        "access_token_enc": _encrypt(str(token["access_token"])),
        "expires_at": expires_at,
        "status": "active",
    }
    if token.get("refresh_token"):
        fields["refresh_token_enc"] = _encrypt(str(token["refresh_token"]))
    await db().integrations.update_one(
        {"user_id": user_id, "provider": "google"},
        {"$set": fields, "$setOnInsert": {"user_id": user_id, "connected_at": utcnow()}},
        upsert=True,
    )
    doc = await db().integrations.find_one({"user_id": user_id, "provider": "google"})
    return Integration(**doc)


async def google_for_user(user_id: Any) -> Integration | None:
    doc = await db().integrations.find_one({"user_id": user_id, "provider": "google"})
    return Integration(**doc) if doc else None


async def is_google_connected(user_id: Any) -> bool:
    integration = await google_for_user(user_id)
    return bool(integration and integration.status == "active")


async def google_access_token(user_id: Any, required_scope: str | None = None) -> str | None:
    integration = await google_for_user(user_id)
    if not integration or integration.status != "active":
        return None
    if required_scope and required_scope not in integration.scopes:
        return None

    if integration.expires_at and integration.expires_at > utcnow() + timedelta(seconds=60):
        return _decrypt(integration.access_token_enc)

    refresh_token = _decrypt(integration.refresh_token_enc)
    if not refresh_token:
        await _mark_expired(user_id)
        return None
    try:
        response = await http().post(
            TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        response.raise_for_status()
        token = response.json()
        token.setdefault("refresh_token", refresh_token)
        token.setdefault("scope", " ".join(integration.scopes))
        await save_google(user_id, token)
        return str(token["access_token"])
    except Exception as exc:
        log.warning("Google token refresh failed for user %s: %s", user_id, type(exc).__name__)
        await _mark_expired(user_id)
        return None


async def _mark_expired(user_id: Any) -> None:
    await db().integrations.update_one(
        {"user_id": user_id, "provider": "google"}, {"$set": {"status": "expired"}}
    )
