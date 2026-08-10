"""One handler for every kind of message. Text, voice, photo and documents take the identical
path — same loop, same prompt, same tools. No commands are ever registered."""

import asyncio
import logging
import secrets
import time

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from atlas.agent import actions, background, context as context_builder, loop
from atlas.bot import onboarding
from atlas.bot.inbound import Inbound, normalize
from atlas.bot.outbound import StreamingReply
from atlas.config import settings
from atlas.db import messages as messages_repo
from atlas.db import traces as traces_repo
from atlas.db import integrations as integrations_repo
from atlas.db import users as users_repo

log = logging.getLogger(__name__)

SORRY = "Something broke on my side just then. Say that again?"
ACCESS_GRANTED = "Access granted! Welcome to the Atlas evaluation preview."
ACCESS_RESTRICTED = "Access restricted. Please send the evaluation passcode to use Atlas."
EVALUATORS_ONLY = "Sorry, this preview build of Atlas is restricted to authorized evaluators."


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    inbound = await normalize(update, ctx.bot)
    if inbound is None:
        return

    reply = StreamingReply(ctx.bot, inbound.chat_id)
    await reply.typing()

    try:
        await _handle(inbound, reply)
    except Exception:
        log.exception("turn failed for chat %s", inbound.chat_id)
        await reply.finish(SORRY)


async def _handle(inbound: Inbound, reply: StreamingReply) -> None:
    user = await users_repo.get_or_create(inbound.chat_id, inbound.telegram_user_id, inbound.first_name)
    if not await _access_allowed(inbound, user, reply):
        return

    first_turn = not await messages_repo.has_history(user.id)
    prior_history = await context_builder.history(user)

    text = inbound.text
    if inbound.note:
        text = f"{text}\n\n[{inbound.note}]".strip()
    if inbound.filename:
        text = f"{text}\n\n[attached file: {inbound.filename}]".strip()

    message_id = await messages_repo.append(
        user.id, "user", text or f"({inbound.media_kind})",
        media_kind=inbound.media_kind, media_ref=inbound.media_ref,
    )

    connected = await integrations_repo.is_google_connected(user.id)
    started = time.monotonic()
    onboarding_text = await onboarding.handle(
        user,
        inbound,
        first_turn=first_turn,
        google_connected=connected,
    )
    if onboarding_text is not None:
        final = await reply.finish(onboarding_text)
        await messages_repo.append(user.id, "assistant", final)
        await traces_repo.write(
            user_id=user.id,
            message_id=message_id,
            rounds=0,
            tool_calls=[],
            total_ms=int((time.monotonic() - started) * 1000),
            provider="onboarding",
            reply=final,
        )
        asyncio.create_task(background.after_turn(user, text, final))
        return

    pending = await actions.pending_for_user(user.id)
    pending_summary = pending.summary if pending else ""
    if pending and not inbound.media:
        decision = await actions.classify_reply(text, pending.summary)
        if decision == "confirm":
            started = time.monotonic()
            action_result = await actions.confirm_and_execute(user, pending)
            final = await reply.finish(_action_reply(action_result))
            await messages_repo.append(user.id, "assistant", final)
            await traces_repo.write(
                user_id=user.id,
                message_id=message_id,
                rounds=0,
                tool_calls=[],
                total_ms=int((time.monotonic() - started) * 1000),
                provider="action",
                reply=final,
                action={"proposed": False, "confirmed": True, "executed": bool(action_result.get("ok")),
                        "pending_action_id": pending.id, "rows_written": action_result.get("rows_written", 0)},
            )
            asyncio.create_task(background.after_turn(user, text, final))
            return

    turn = context_builder.current_turn(text, inbound.media)
    result = await loop.run_turn(
        user,
        turn,
        on_delta=reply.update,
        connected=connected,
        pending_summary=pending_summary,
        first_turn=first_turn,
        history=prior_history,
    )
    final = await reply.finish(result.text)

    await messages_repo.append(user.id, "assistant", final)
    await traces_repo.write(
        user_id=user.id,
        message_id=message_id,
        rounds=result.rounds,
        tool_calls=result.tool_calls,
        total_ms=result.total_ms,
        provider=result.provider,
        reply=final,
        tokens=result.tokens,
        action={"proposed": bool(result.proposed_action_id), "confirmed": False, "executed": False,
                "pending_action_id": result.proposed_action_id},
    )
    log.info(
        "turn chat=%s rounds=%d tools=%s ms=%d provider=%s",
        inbound.chat_id, result.rounds,
        [t["name"] for t in result.tool_calls], result.total_ms, result.provider,
    )

    # facts and the narrative are refreshed after the reply is out, never before it
    asyncio.create_task(background.after_turn(user, text, final))


async def _access_allowed(inbound: Inbound, user, reply: StreamingReply) -> bool:
    """Resolve the preview gate before history, integrations, onboarding, or LLM work."""
    if not settings.access_control_enabled:
        return True
    if (
        inbound.telegram_user_id in settings.allowed_telegram_user_ids
        or inbound.chat_id in settings.allowed_chat_id_values
        or inbound.telegram_user_id in user.authorized_telegram_user_ids
    ):
        return True

    supplied = inbound.text.strip()
    if settings.tester_passcode and supplied and secrets.compare_digest(
        supplied, settings.tester_passcode
    ):
        await users_repo.authorize_user(user.id, inbound.telegram_user_id)
        await reply.finish(ACCESS_GRANTED)
        return False

    await reply.finish(ACCESS_RESTRICTED if settings.tester_passcode else EVALUATORS_ONLY)
    return False


def _action_reply(result: dict) -> str:
    if result.get("kind") == "calendar_event":
        if result.get("ok"):
            summary = str(result.get("summary") or "Calendar event")
            start = result.get("start") or "the requested time"
            if isinstance(start, dict):
                start = start.get("dateTime") or start.get("date") or "the requested time"
            link = str(result.get("html_link") or "")
            suffix = f" [Open in Google Calendar]({link})." if link else ""
            return f"Scheduled {summary} for {start}.{suffix}"
        error = str(result.get("error") or "Google Calendar rejected the event.")
        return f"I didn't create the calendar event: {error} Say retry after reconnecting and I'll continue."
    written = int(result.get("rows_written") or 0)
    if result.get("ok"):
        return f"Done — {written} rows written to {result.get('target', 'the sheet')}."
    error = str(result.get("error") or "Google rejected the action.")
    if written:
        return f"I wrote {written} rows, then stopped: {error} Say retry and I'll resume without duplicating them."
    return f"I didn't write anything: {error} Say retry after reconnecting and I'll continue."


def register(app: Application) -> None:
    """Exactly one handler. No CommandHandler, no set_my_commands, no keyboards — ever."""
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.VOICE | filters.AUDIO | filters.PHOTO | filters.Document.ALL,
            on_message,
        )
    )
