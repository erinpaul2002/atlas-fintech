"""Pydantic-on-read models. Every field has a default — there are no migrations, so an old
document missing a newer field must never crash a handler (DATA_MODEL.md, preamble)."""

from datetime import datetime, timezone
from typing import Any, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Doc(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True, extra="ignore")

    id: Any = Field(default=None, alias="_id")


class WatchlistItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str = ""
    company_name: str = ""
    reason: str = ""
    added_at: datetime = Field(default_factory=utcnow)


class Interests(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sectors: list[str] = []
    topics: list[str] = []
    markets: list[str] = []


class User(Doc):
    telegram_chat_id: int = 0
    telegram_user_id: int = 0
    is_authorized: bool = False
    # A chat may contain multiple Telegram users. Keep passcode grants per sender so one
    # evaluator entering the passcode does not unlock a public group for everyone.
    authorized_telegram_user_ids: list[int] = []
    first_name: str = ""
    role: str = ""
    timezone: str = "UTC"
    brief_hour_local: int | None = None
    brief_minute_local: int = 0
    interests: Interests = Interests()
    intel_preferences: list[str] = []
    preferences: dict[str, Any] = {}
    onboarding_status: Literal["new", "in_progress", "completed", "skipped"] = "new"
    onboarding_step: str = "choice"
    onboarding_offered: list[str] = []
    google_offer_shown: bool = False
    profile_narrative: str = ""
    profile_narrative_at: datetime | None = None
    watchlist: list[WatchlistItem] = []
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def unknowns(self) -> list[str]:
        """Profile gaps learned naturally after explicit first-run setup."""
        gaps = []
        if not self.role:
            gaps.append("role")
        if not (self.interests.sectors or self.interests.topics or self.interests.markets):
            gaps.append("interests")
        if not self.intel_preferences:
            gaps.append("what's worth interrupting them for")
        if self.brief_hour_local is None:
            gaps.append("when the morning brief should land")
        return [gap for gap in gaps if gap not in self.onboarding_offered]


class Message(Doc):
    user_id: Any = None
    role: Literal["user", "assistant", "tool"] = "user"
    content: str = ""
    tool_name: str | None = None
    media_kind: str | None = None
    media_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Fact(Doc):
    user_id: Any = None
    kind: str = "context"
    text: str = ""
    source_message_id: Any = None
    confidence: float = 1.0
    superseded_by: Any = None
    created_at: datetime = Field(default_factory=utcnow)


class Alert(Doc):
    user_id: Any = None
    kind: str = "price_move"
    symbol: str | None = None
    params: dict[str, Any] = {}
    natural_language: str = ""
    active: bool = True
    last_fired_at: datetime | None = None
    last_checked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Document(Doc):
    user_id: Any = None
    filename: str = ""
    mime_type: str = ""
    origin: str = "upload"
    telegram_file_id: str | None = None
    source_url: str | None = None
    extracted_text: str = ""
    page_count: int = 0
    summary: str = ""
    digest: dict[str, Any] = {}
    created_at: datetime = Field(default_factory=utcnow)


class PendingAction(Doc):
    user_id: Any = None
    kind: str = "sheet_write"
    idempotency_key: str = ""
    spec: dict[str, Any] = {}
    summary: str = ""
    status: str = "pending"
    rows_written: int = 0
    error: str | None = None
    proposed_at: datetime | None = None
    confirmed_at: datetime | None = None
    completed_at: datetime | None = None


class Integration(Doc):
    user_id: Any = None
    provider: str = "google"
    scopes: list[str] = []
    access_token_enc: bytes | None = None
    refresh_token_enc: bytes | None = None
    expires_at: datetime | None = None
    status: str = "active"
    connected_at: datetime = Field(default_factory=utcnow)


def oid(value: Any) -> ObjectId | None:
    """Tolerant ObjectId coercion — ids cross the model boundary as str in tool args."""
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception:
        return None
