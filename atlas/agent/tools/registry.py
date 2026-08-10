"""The single place tools are declared. Name → (schema, callable).

Tool descriptions are part of the prompt — they are how the model chooses between a sheet, a
filing and a news pull. Write them for a reader, not for a schema validator.

Every tool returns {"data": ..., "source": str, "as_of": iso8601} or {"error": str}.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from google.genai import types

from atlas.db.models import User
from atlas.services.util import now_iso

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 30.0


@dataclass
class ToolContext:
    """What a tool knows about the turn it's serving."""

    user: User
    extra: dict[str, Any] = field(default_factory=dict)


ToolFn = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable[[ToolFn], ToolFn]:
    def register(fn: ToolFn) -> ToolFn:
        REGISTRY[name] = ToolSpec(name=name, description=description.strip(), parameters=parameters, fn=fn)
        return fn

    return register


def ok(data: Any, source: str, as_of: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"data": data, "source": source, "as_of": as_of or now_iso(), **extra}


def declarations() -> list[types.Tool]:
    """One Tool holding every function declaration.

    Google Search grounding is deliberately absent: Gemini rejects built-in tools alongside
    function declarations (verified 6 Aug 2026), so web_search issues its own grounded call.
    """
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name=s.name, description=s.description, parameters=s.parameters)
                for s in REGISTRY.values()
            ]
        )
    ]


async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any], int, bool]:
    """Run one tool. Returns (result, elapsed_ms, ok). Never raises into the conversation."""
    started = time.monotonic()
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": f"unknown tool {name}"}, 0, False
    try:
        result = await asyncio.wait_for(spec.fn(ctx, args or {}), TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        result = {"error": f"{name} took too long and was cut off"}
    except Exception as exc:
        log.exception("tool %s failed", name)
        result = {"error": f"{name} failed: {type(exc).__name__}"}
    ms = int((time.monotonic() - started) * 1000)
    return result, ms, "error" not in result


# populate the registry — import for side effects, last so the decorator exists first
from atlas.agent.tools import (  # noqa: E402,F401
    filings,
    google,
    google_calendar,
    google_content,
    market,
    memory,
    research,
    search,
)
