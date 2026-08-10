"""Google OAuth redirect and callback endpoints."""

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from atlas.db import integrations
from atlas.services import google

router = APIRouter(prefix="/oauth/google", tags=["google"])


@router.get("/start")
async def start(state: str = Query(min_length=20)):
    if not await integrations.state_exists(state):
        return HTMLResponse("This connection link expired. Ask Atlas for a new one.", status_code=400)
    return RedirectResponse(google.authorization_url(state))


@router.get("/callback")
async def callback(
    state: str = Query(min_length=20),
    code: str | None = None,
    error: str | None = None,
):
    user_id = await integrations.consume_oauth_state(state)
    if user_id is None:
        return HTMLResponse("This connection link is invalid or expired.", status_code=400)
    if error or not code:
        return HTMLResponse("Google connection was cancelled. You can close this tab.", status_code=400)

    token = await google.exchange_code(code)
    if "error" in token:
        return HTMLResponse("Google could not be connected. Ask Atlas for a fresh link.", status_code=502)
    await integrations.save_google(user_id, token)
    return HTMLResponse(
        "<h2>Google connected</h2><p>You can close this tab and return to Atlas in Telegram.</p>"
    )
