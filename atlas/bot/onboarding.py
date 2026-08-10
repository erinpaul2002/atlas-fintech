"""A short, resumable first-run conversation with no buttons or forms."""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from atlas.agent.tools.google import connection_link
from atlas.agent.tools.registry import ToolContext
from atlas.bot.inbound import Inbound
from atlas.db import users as users_repo
from atlas.db.models import User

FIRST_GREETING = (
    "Hey{name} — I'm Atlas, a financial analyst you can text here.\n"
    "I track live markets, compare companies, and research earnings and SEC filings.\n"
    "I can analyze documents, charts, images, and voice notes without turning the reply into a report.\n"
    "With one Google connection, I can work with Sheets, Gmail, Calendar, and Drive.\n"
    "Want to set things up now, or skip onboarding and start chatting?"
)

SKIPPED = (
    "No problem — onboarding skipped. Ask me about a market, company, filing, document, or chart.\n"
    "If something needs Google, I'll give you a secure connection link only when it's required."
)

COMPLETE = (
    "You're set. I can now tailor market research, filing analysis, and concise answers to what matters to you.\n"
    "Send a company question, document, chart, or voice note whenever you're ready."
)

COMPLETE_WITH_GOOGLE = (
    "Setup complete — Google is connected. I can now read or write Sheets, search Gmail, read "
    "Drive files, and schedule or review Calendar events alongside market research.\n"
    "Send me a company question, document, chart, or the Sheet you want to work with."
)

QUESTIONS = {
    "choice": "Would you like to set up Atlas now, or skip onboarding and start chatting?",
    "role": (
        "First, what best describes your work — analyst, investor, founder, portfolio manager, "
        "or something else?"
    ),
    "interests": "What companies, sectors, topics, or markets matter most to you?",
    "intel_preferences": (
        "What is important enough for me to proactively flag — earnings, filings, major news, "
        "price moves, or something else?"
    ),
    "brief_time": (
        "When should a concise daily brief arrive? Include the time and timezone, or say “no brief.”"
    ),
    "google": (
        "One Google connection enables private Sheets read/write, Gmail search, Drive document "
        "reading, and Calendar scheduling. Connect now, or skip Google for now?"
    ),
    "google_pending": (
        "Finish the Google consent in the link I sent, then say “connected” — or say “skip Google.”"
    ),
}

PROFILE_STEPS = ("role", "interests", "intel_preferences", "brief_time")
YES = re.compile(
    r"^(yes|yep|yeah|sure|ok(?:ay)?|now|start|setup(?: now)?|connect(?: now)?|"
    r"let'?s do it|set (?:it|me) up)\b",
    re.I,
)
NO = re.compile(r"^(no|nope|not now|later|skip|skip onboarding|skip setup|no thanks)\b", re.I)
UNKNOWN = re.compile(
    r"^(idk|i don'?t know|not sure|no preference|nothing specific|whatever(?: is important)?)\.?$",
    re.I,
)
NO_BRIEF = re.compile(r"\b(no brief|skip (?:the )?brief|don'?t (?:send|want).*(?:brief|update))\b", re.I)
TIME = re.compile(r"\b([01]?\d|2[0-3])(?::([0-5]\d))?\s*(am|pm)?\b", re.I)
IANA_ZONE = re.compile(r"\b([A-Za-z]+/[A-Za-z_+-]+)\b")

ZONE_ALIASES = {
    "ist": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "india time": "Asia/Kolkata",
    "indian time": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "new york": "America/New_York",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "utc": "UTC",
    "gmt": "UTC",
}


async def handle(
    user: User,
    inbound: Inbound,
    *,
    first_turn: bool,
    google_connected: bool,
) -> str | None:
    """Return an onboarding reply when this turn belongs to setup; otherwise yield to chat."""
    status = user.onboarding_status
    step = user.onboarding_step or "choice"

    if status in {"completed", "skipped"}:
        return None

    if status == "new":
        if not _starts_setup(inbound, first_turn):
            return None
        await users_repo.set_onboarding(user.id, "in_progress", "choice")
        name = f" {user.first_name}" if user.first_name else ""
        return FIRST_GREETING.format(name=name)

    if inbound.is_start:
        if step == "google_pending" and google_connected:
            await users_repo.set_onboarding(user.id, "completed", "done")
            return COMPLETE_WITH_GOOGLE
        return QUESTIONS.get(step, QUESTIONS["choice"])

    text = inbound.text.strip()
    if inbound.media or not text:
        return QUESTIONS.get(step, QUESTIONS["choice"])

    if step == "choice":
        if NO.search(text):
            await users_repo.set_onboarding(user.id, "skipped", "done")
            return SKIPPED
        if not YES.search(text):
            return QUESTIONS["choice"]
        next_step = _first_profile_step(user)
        await users_repo.set_onboarding(user.id, "in_progress", next_step)
        return QUESTIONS[next_step]

    if step == "google_pending":
        if google_connected:
            await users_repo.set_onboarding(user.id, "completed", "done")
            return COMPLETE_WITH_GOOGLE
        if NO.search(text):
            await users_repo.set_onboarding(user.id, "completed", "done")
            return COMPLETE
        if YES.search(text) or re.search(r"\blink\b", text, re.I):
            return await _google_link(user)
        return QUESTIONS["google_pending"]

    if re.search(r"\bskip (?:onboarding|setup)\b", text, re.I) or re.fullmatch(
        r"(?:skip|no|nope|not now|later|no thanks)", text, re.I
    ):
        await users_repo.set_onboarding(user.id, "skipped", "done")
        return SKIPPED

    if step in PROFILE_STEPS and UNKNOWN.fullmatch(text):
        gap = {
            "role": "role",
            "interests": "interests",
            "intel_preferences": "what's worth interrupting them for",
            "brief_time": "when the morning brief should land",
        }[step]
        await users_repo.mark_onboarding_offered(user.id, [gap])
        return await _advance(user, step)

    if step == "role":
        updated = await users_repo.patch(user.id, {"role": text[:160]}) or user
        await users_repo.mark_onboarding_offered(user.id, ["role"])
        return await _advance(updated, step)

    if step == "interests":
        items = _items(text)
        updated = await users_repo.patch(
            user.id,
            {"interests": {"sectors": [], "topics": items, "markets": []}},
        ) or user
        await users_repo.mark_onboarding_offered(user.id, ["interests"])
        return await _advance(updated, step)

    if step == "intel_preferences":
        updated = await users_repo.patch(user.id, {"intel_preferences": _items(text)}) or user
        await users_repo.mark_onboarding_offered(user.id, ["what's worth interrupting them for"])
        return await _advance(updated, step)

    if step == "brief_time":
        if NO_BRIEF.search(text):
            await users_repo.mark_onboarding_offered(
                user.id, ["when the morning brief should land"]
            )
            return await _advance(user, step)
        parsed = _brief_time(text, user.timezone)
        if parsed is None:
            return QUESTIONS["brief_time"]
        hour, minute, timezone = parsed
        updated = await users_repo.patch(
            user.id,
            {
                "brief_hour_local": hour,
                "brief_minute_local": minute,
                "timezone": timezone,
            },
        ) or user
        await users_repo.mark_onboarding_offered(
            user.id, ["when the morning brief should land"]
        )
        next_question = await _advance(updated, step)
        return f"Daily brief set for {_display_time(hour, minute, timezone)}.\n\n{next_question}"

    if step == "google":
        if google_connected:
            await users_repo.set_onboarding(user.id, "completed", "done")
            return COMPLETE_WITH_GOOGLE
        if NO.search(text):
            await users_repo.set_onboarding(user.id, "completed", "done")
            return COMPLETE
        if YES.search(text):
            await users_repo.set_onboarding(user.id, "in_progress", "google_pending")
            return await _google_link(user)
        return QUESTIONS["google"]

    await users_repo.set_onboarding(user.id, "in_progress", "choice")
    return QUESTIONS["choice"]


def _starts_setup(inbound: Inbound, first_turn: bool) -> bool:
    if inbound.media:
        return False
    if inbound.is_start:
        return True
    return first_turn and inbound.text.strip().lower() in {"hi", "hello", "hey"}


def _first_profile_step(user: User) -> str:
    for step in PROFILE_STEPS:
        if _needs_profile_step(user, step):
            return step
    return "google"


async def _advance(user: User, current: str) -> str:
    index = PROFILE_STEPS.index(current)
    for candidate in PROFILE_STEPS[index + 1 :]:
        if _needs_profile_step(user, candidate):
            await users_repo.set_onboarding(user.id, "in_progress", candidate)
            return QUESTIONS[candidate]
    await users_repo.set_onboarding(user.id, "in_progress", "google")
    return QUESTIONS["google"]


async def _google_link(user: User) -> str:
    url = await connection_link(ToolContext(user=user))
    return (
        f"🔗 [Connect Google securely]({url})\n"
        "This one-time link connects Sheets, Gmail, Calendar, and Drive. "
        "I'll confirm here automatically when it's done."
    )


def _needs_profile_step(user: User, step: str) -> bool:
    if step == "role":
        return not user.role
    if step == "interests":
        return not (user.interests.sectors or user.interests.topics or user.interests.markets)
    if step == "intel_preferences":
        return not user.intel_preferences
    if step == "brief_time":
        return user.brief_hour_local is None
    return False


def _items(text: str) -> list[str]:
    pieces = re.split(r",|;|\n|\s+and\s+", text, flags=re.I)
    return list(dict.fromkeys(piece.strip(" .") for piece in pieces if piece.strip(" .")))[:12]


def _brief_time(text: str, current_timezone: str) -> tuple[int, int, str] | None:
    match = TIME.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0

    timezone = _timezone(text)
    if timezone is None and current_timezone != "UTC":
        timezone = current_timezone
    if timezone is None:
        return None
    return hour, minute, timezone


def _timezone(text: str) -> str | None:
    iana = IANA_ZONE.search(text)
    candidate = iana.group(1) if iana else None
    lowered = text.lower()
    if candidate is None:
        candidate = next((zone for label, zone in ZONE_ALIASES.items() if _has_label(lowered, label)), None)
    if candidate is None:
        return None
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return candidate


def _display_time(hour: int, minute: int, timezone: str) -> str:
    meridiem = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    label = "IST" if timezone == "Asia/Kolkata" else timezone
    return f"{display_hour}:{minute:02d} {meridiem} {label}"


def _has_label(text: str, label: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(label)}(?!\w)", text))
