"""Google OAuth redirect and callback endpoints."""

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from atlas.api.oauth_ui import cancelled_page, connected_page, expired_page, failed_page
from atlas.db import integrations
from atlas.services import google

router = APIRouter(prefix="/oauth/google", tags=["google"])


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
    return RedirectResponse(
        url="/oauth/google/complete",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
