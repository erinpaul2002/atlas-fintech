"""Per-turn context. Assembled fresh on every message — AGENT_SPEC.md §1.

Nothing is cached across turns except what's in the database.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.genai import types

from atlas.agent.prompt import PROMPT
from atlas.db import facts as facts_repo
from atlas.db import messages as messages_repo
from atlas.db.models import User

HISTORY_TURNS = 30


def local_now(user: User) -> datetime:
    try:
        return datetime.now(ZoneInfo(user.timezone or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(ZoneInfo("UTC"))


async def system_instruction(
    user: User,
    connected: bool = False,
    pending_summary: str = "",
    first_turn: bool = False,
) -> str:
    lines = [PROMPT, "", "--- who you're talking to ---"]
    when = local_now(user)
    lines.append(f"name: {user.first_name or 'unknown'}")
    lines.append(f"role: {user.role or 'unknown'}")
    lines.append(f"onboarding: {user.onboarding_status}")
    lines.append(f"their local time right now: {when:%A %d %b %Y, %H:%M} ({user.timezone})")
    lines.append(
        "when asked for the current time: repeat the local time above exactly; never calculate a different time"
    )
    lines.append(
        "privacy default: minimize personal data from connected sources; do not volunteer phone numbers, "
        "email addresses, banking/identity filenames, or similar sensitive details unless they explicitly ask"
    )
    if first_turn:
        lines.append(
            "this is their first message: answer a real question first; otherwise briefly describe "
            "Atlas and ask whether they want to set up now or skip onboarding"
        )

    if user.profile_narrative:
        lines.append(f"who they are: {user.profile_narrative}")

    interests = ", ".join(user.interests.sectors + user.interests.topics + user.interests.markets)
    if interests:
        lines.append(f"interests: {interests}")
    if user.intel_preferences:
        lines.append(f"worth interrupting them for: {', '.join(user.intel_preferences)}")
    if user.brief_hour_local is not None:
        lines.append(f"morning brief at: {user.brief_hour_local:02d}:{user.brief_minute_local:02d} local")

    if user.watchlist:
        lines.append("watchlist:")
        for w in user.watchlist[:25]:
            reason = f" ({w.reason})" if w.reason else ""
            lines.append(f"  {w.symbol} — {w.company_name or w.symbol}{reason}")

    recent_facts = await facts_repo.current(user.id, limit=60)
    if recent_facts:
        lines.append("recent facts, newest first:")
        lines += [f"  {f.text}" for f in recent_facts]

    unknowns = user.unknowns()
    if unknowns:
        lines.append(f"still unknown: {' | '.join(unknowns)}")

    lines.append(f"connected: {'google (sheets rw, gmail, calendar, drive)' if connected else 'nothing connected yet'}")
    if not connected and user.google_offer_shown:
        lines.append("google connection was already offered once; do not offer it again unless they ask")

    if pending_summary:
        lines += ["", "--- pending ---", f"awaiting their confirmation: {pending_summary}"]

    return "\n".join(lines)


async def history(user: User) -> list[types.Content]:
    turns = await messages_repo.recent(user.id, limit=HISTORY_TURNS)
    out: list[types.Content] = []
    for m in turns:
        if not m.content.strip():
            continue
        out.append(
            types.Content(
                role="model" if m.role == "assistant" else "user",
                parts=[types.Part(text=m.content)],
            )
        )
    return out


def current_turn(text: str, media: list[tuple[str, bytes]] | None = None) -> types.Content:
    """Media attaches straight to the model call — no transcription or OCR step."""
    parts: list[types.Part] = []
    for mime, data in media or []:
        parts.append(types.Part(inline_data=types.Blob(mime_type=mime, data=data)))
    parts.append(types.Part(text=text or "(no text — see the attached file)"))
    return types.Content(role="user", parts=parts)
