"""Public, unguessable links for Atlas-generated visuals."""

import re

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from atlas.db import visuals
from atlas.visuals.render import CONTENT_SECURITY_POLICY

router = APIRouter()
VISUAL_ID = re.compile(r"^v_[A-Za-z0-9_-]{16}$")


def _headers(content_security_policy: str = CONTENT_SECURITY_POLICY) -> dict[str, str]:
    return {
        "Cache-Control": "private, max-age=300",
        "Content-Security-Policy": content_security_policy,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }


@router.get("/v/{visual_id}", response_class=HTMLResponse, include_in_schema=False)
async def view_visual(visual_id: str) -> HTMLResponse:
    document = await visuals.get_visual(visual_id) if VISUAL_ID.fullmatch(visual_id) else None
    if not document:
        return HTMLResponse(
            "<!doctype html><meta name=viewport content='width=device-width'>"
            "<title>Visual unavailable · Atlas</title>"
            "<style>body{margin:0;background:#07100d;color:#f5f1e8;font:18px Georgia,serif;"
            "display:grid;min-height:100vh;place-items:center}main{max-width:34rem;padding:2rem}"
            "small{color:#9cab9f;font:13px ui-monospace,monospace}</style>"
            "<main><small>ATLAS · VISUAL</small><h1>This visual is unavailable.</h1>"
            "<p>The link may have expired. Ask Atlas to generate a fresh view.</p></main>",
            status_code=404,
            headers=_headers(),
        )
    return HTMLResponse(
        document["html_content"],
        headers=_headers(document.get("content_security_policy", CONTENT_SECURITY_POLICY)),
    )
