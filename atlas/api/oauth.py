"""Google OAuth redirect and callback endpoints."""

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from atlas.api.oauth_ui import cancelled_page, connected_page, expired_page, failed_page
from atlas.db import integrations
from atlas.db import messages as messages_repo
from atlas.db import users as users_repo
from atlas.services import google

router = APIRouter(prefix="/oauth/google", tags=["google"])
log = logging.getLogger(__name__)

CONNECTED_MESSAGE = (
    "Google connected ✓\n"
    "Sheets, Gmail, Calendar, and Drive are ready. Try “show my latest email” or send me a Sheet."
)


@router.get("/start")
async def start(state: str = Query(min_length=20)):
    if not await integrations.state_exists(state):
        return expired_page()
    return RedirectResponse(google.authorization_url(state))


@router.get("/complete")
async def complete():
    return connected_page()


@router.get("/callback")
async def callback(
    request: Request,
    state: str = Query(min_length=20),
    code: str | None = None,
    error: str | None = None,
):
    user_id = await integrations.consume_oauth_state(state)
    if user_id is None:
        return expired_page()
    if error or not code:
        return cancelled_page()

    token = await google.exchange_code(code)
    if "error" in token:
        return failed_page()
    await integrations.save_google(user_id, token)
    await _finish_onboarding_and_notify(request, user_id)
    return RedirectResponse(
        url="/oauth/google/complete",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def _finish_onboarding_and_notify(request: Request, user_id) -> None:
    """Make the browser consent feel complete without requiring another chat message."""
    try:
        user = await users_repo.by_id(user_id)
        if user is None:
            return
        if user.onboarding_status in {"new", "in_progress"}:
            await users_repo.set_onboarding(user.id, "completed", "done")

        bot = getattr(request.app.state, "bot", None)
        if bot is None or not user.telegram_chat_id:
            return
        await bot.send_message(user.telegram_chat_id, CONNECTED_MESSAGE)
        await messages_repo.append(user.id, "assistant", CONNECTED_MESSAGE)
    except Exception as exc:
        # OAuth succeeded already; a Telegram notification failure must not undo it.
        log.warning("Google connected but completion notification failed: %s", str(exc)[:160])
