"""GET /health — pings Mongo and Telegram, so a failure is visible without reading logs."""

from fastapi import APIRouter, Request, Response

from atlas.db.client import ping

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, object]:
    checks: dict[str, object] = {}

    try:
        await ping()
        checks["mongo"] = "ok"
    except Exception as exc:
        checks["mongo"] = f"down: {type(exc).__name__}"

    bot = getattr(request.app.state, "bot", None)
    try:
        me = await bot.get_me()
        checks["telegram"] = f"ok: @{me.username}"
    except Exception as exc:
        checks["telegram"] = f"down: {type(exc).__name__}"

    healthy = all(str(v).startswith("ok") for v in checks.values())
    response.status_code = 200 if healthy else 503
    return {"status": "ok" if healthy else "degraded", **checks}
