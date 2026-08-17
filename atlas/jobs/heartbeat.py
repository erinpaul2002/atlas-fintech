"""One scheduler entrypoint; due work remains durable in MongoDB."""

import asyncio
import logging
from typing import Any

from atlas.jobs import alerts, morning_brief

log = logging.getLogger(__name__)


async def run(bot: Any) -> None:
    results = await asyncio.gather(
        alerts.run(bot),
        morning_brief.run(bot),
        return_exceptions=True,
    )
    for name, result in zip(("alerts", "morning_brief"), results):
        if isinstance(result, Exception):
            log.exception("%s heartbeat failed", name, exc_info=result)
