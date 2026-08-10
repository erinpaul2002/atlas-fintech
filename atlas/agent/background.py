"""After the reply is sent: pull durable facts out of the exchange, refresh the narrative.

Runs detached from the turn, so nothing here is allowed to be on the user's critical path or
to raise into it. The model is told to call remember() itself; this is the safety net for the
things it doesn't notice.
"""

import json
import logging
import re

from atlas.db import facts as facts_repo
from atlas.db import users as users_repo
from atlas.db.models import User
from atlas.llm import provider

log = logging.getLogger(__name__)

FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["interest", "position", "preference", "context", "person"]},
                },
                "required": ["text", "kind"],
            },
        }
    },
    "required": ["facts"],
}

EXTRACT_SYSTEM = """You extract durable facts about a person from one exchange with their
financial assistant.

Durable means still true next month: their role, employer, holdings, sectors and companies they
follow, how they like to be told things, views they hold, people they work with.

Not durable: what they asked today, anything the assistant said, prices, one-off questions,
pleasantries. One claim per fact. Third person, under 12 words. Return an empty list if nothing
in the exchange is durable — that is the common case."""

NARRATIVE_SYSTEM = """You write a one-paragraph description of a person for their financial
assistant to read before every conversation.

Around 120 words, prose, no lists, no headings. Concrete and specific: what they do, what they
follow, what they hold, how they want to be talked to. It should read like a colleague
describing them, because they will ask "what do you know about me?" and this is the answer.
Only what the facts support — invent nothing."""


async def after_turn(user: User, user_text: str, assistant_text: str) -> None:
    try:
        await _record_offered_gaps(user, assistant_text)
        await _extract_facts(user, user_text, assistant_text)
        await _refresh_narrative(user)
    except Exception as exc:  # never surfaces to the user
        log.warning("background pass failed for %s: %s", user.id, exc)


# A durable fact about a person nearly always arrives with a first-person marker. Screening on
# it drops the extraction call from most turns ("how's Nvidia trading" has nothing in it), which
# matters because Gemini's free tier is 10 requests/minute and a turn already costs two.
# ponytail: regex, not a classifier — the model can still call remember() for what this misses.
FIRST_PERSON = re.compile(r"\b(i|i'?m|i'?ve|my|me|mine|we|we'?re|our|us)\b", re.I)

OFFER_PATTERNS = {
    "role": re.compile(r"\b(what do you do|analyst,? investor,? founder|your role)\b", re.I),
    "interests": re.compile(r"\b(what (?:markets|sectors|companies|areas).*follow|what do you follow|focus on)\b", re.I),
    "what's worth interrupting them for": re.compile(
        r"\b(worth interrupting|what should i (?:flag|alert|ping)|worth a ping)\b", re.I
    ),
    "when the morning brief should land": re.compile(
        r"\b(what time.*(?:brief|update)|when.*morning brief|brief.*what time)\b", re.I
    ),
}


async def _record_offered_gaps(user: User, assistant_text: str) -> None:
    if "?" not in assistant_text:
        return
    offered = [gap for gap, pattern in OFFER_PATTERNS.items() if pattern.search(assistant_text)]
    await users_repo.mark_onboarding_offered(user.id, offered[:1])


async def _extract_facts(user: User, user_text: str, assistant_text: str) -> None:
    if len(user_text.strip()) < 6 or not FIRST_PERSON.search(user_text):
        return
    existing = await facts_repo.current(user.id, limit=80)
    known = "\n".join(f"- {f.text}" for f in existing) or "(nothing yet)"

    raw = await provider.generate(
        system=EXTRACT_SYSTEM,
        prompt=(
            f"Already known about them:\n{known}\n\n"
            f"They said:\n{user_text}\n\n"
            f"The assistant replied:\n{assistant_text[:1500]}\n\n"
            "Return only facts that are genuinely new — not a rewording of something already known."
        ),
        temperature=0.1,
        json_schema=FACT_SCHEMA,
    )
    if not raw:
        return
    try:
        parsed = json.loads(raw).get("facts", [])
    except json.JSONDecodeError:
        return

    seen = {f.text.lower().strip() for f in existing}
    for item in parsed[:5]:
        text = str(item.get("text", "")).strip()
        if text and text.lower() not in seen:
            await facts_repo.add(user.id, text=text, kind=str(item.get("kind", "context")), confidence=0.8)
            seen.add(text.lower())


async def _refresh_narrative(user: User) -> None:
    if not await facts_repo.narrative_is_stale(user.id):
        return
    live = await facts_repo.current(user.id, limit=120)
    if len(live) < 2:
        return
    narrative = await provider.generate(
        system=NARRATIVE_SYSTEM,
        prompt=f"Their name is {user.first_name or 'unknown'}. Role: {user.role or 'unknown'}.\n"
        + "Facts, newest first:\n"
        + "\n".join(f"- {f.text}" for f in live),
        temperature=0.3,
    )
    if narrative.strip():
        await users_repo.set_narrative(user.id, narrative.strip())
