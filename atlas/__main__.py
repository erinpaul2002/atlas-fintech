"""Entrypoint: FastAPI app with PTB polling running inside its lifespan. One process."""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from telegram.ext import Application

from atlas.api.health import router as health_router
from atlas.api.oauth import router as oauth_router
from atlas.bot import handlers
from atlas.config import settings
from atlas.db.client import close as close_db
from atlas.db.client import ensure_indexes
from atlas.llm import provider
from atlas.services import yfinance_client
from atlas.services.util import close_http

log = logging.getLogger("atlas")


def build_bot() -> Application:
    return (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(8)
        .build()
    )


async def _warm() -> None:
    """Pay yfinance's 2.2s cold start and Gemini's first-call latency before a judge does."""
    await asyncio.gather(yfinance_client.warmup(), provider.warmup(), return_exceptions=True)
    log.info("warm-up done")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()

    bot_app = build_bot()
    handlers.register(bot_app)
    app.state.bot_app = bot_app
    app.state.bot = bot_app.bot

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    me = await bot_app.bot.get_me()
    log.info("polling as @%s", me.username)

    warm = asyncio.create_task(_warm())
    try:
        yield
    finally:
        warm.cancel()
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        await close_http()
        await close_db()
        log.info("stopped")


app = FastAPI(title="Atlas", lifespan=lifespan)
app.include_router(health_router)
app.include_router(oauth_router)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
