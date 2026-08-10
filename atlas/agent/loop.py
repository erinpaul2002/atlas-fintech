"""The tool-calling loop. One loop, no router, no sub-agents — ARCHITECTURE.md §4.

Independent calls in a round run concurrently. A tool failure becomes `{"error": ...}` inside
the conversation so the model explains the gap; nothing raises into the chat.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from atlas.agent import context as context_builder
from atlas.agent.tools import registry
from atlas.db.models import User
from atlas.llm import provider

log = logging.getLogger(__name__)

MAX_ROUNDS = 5
EMPTY_REPLY = "I couldn't put that together just now — try me again in a moment."


@dataclass
class TurnResult:
    text: str = ""
    rounds: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    provider: str = "gemini"
    total_ms: int = 0
    proposed_action_id: Any = None


async def run_turn(
    user: User,
    turn: types.Content,
    on_delta=None,
    connected: bool = False,
    pending_summary: str = "",
    first_turn: bool = False,
    history: list[types.Content] | None = None,
) -> TurnResult:
    started = time.monotonic()
    system = await context_builder.system_instruction(
        user, connected=connected, pending_summary=pending_summary, first_turn=first_turn
    )
    contents = list(history if history is not None else await context_builder.history(user))
    contents.append(turn)

    ctx = registry.ToolContext(user=user)
    tools = registry.declarations()
    out = TurnResult()
    result = provider.Result()

    for round_no in range(MAX_ROUNDS):
        out.rounds = round_no + 1
        result = await provider.stream(system, contents, tools, on_delta=on_delta)
        out.provider = result.provider
        _add_tokens(out, result)

        if not result.function_calls:
            out.text = result.text
            break

        contents.append(_model_turn(result))
        responses = await _run_tools(result.function_calls, ctx, out)
        contents.append(types.Content(role="user", parts=responses))
        out.text = ""  # this round was tool traffic; the answer comes from the next one
    else:
        # burned every round on tools — answer with what we have, no tools offered
        log.info("hit the round ceiling for user %s", user.id)
        final = await provider.stream(system, contents, None, on_delta=on_delta)
        out.text = final.text
        _add_tokens(out, final)

    if not out.text.strip():
        out.text = EMPTY_REPLY if result.error or not result.text else result.text
    out.total_ms = int((time.monotonic() - started) * 1000)
    return out


def _model_turn(result: provider.Result) -> types.Content:
    """Replay the model's own turn back to it — its Parts, not copies of them.

    `call_parts` carries each function call exactly as it arrived, thought signature included,
    which is what Gemini 2.5 expects to get back. Only fall back to rebuilding if a provider
    gave us calls without parts.
    """
    parts: list[types.Part] = []
    if result.text.strip():
        parts.append(types.Part(text=result.text))
    parts += result.call_parts or [types.Part(function_call=fc) for fc in result.function_calls]
    return types.Content(role="model", parts=parts)


async def _run_tools(
    calls: list[types.FunctionCall], ctx: registry.ToolContext, out: TurnResult
) -> list[types.Part]:
    results = await asyncio.gather(
        *(registry.dispatch(c.name or "", dict(c.args or {}), ctx) for c in calls)
    )
    parts: list[types.Part] = []
    for call, (payload, ms, succeeded) in zip(calls, results):
        out.tool_calls.append(
            {
                "name": call.name,
                "args": dict(call.args or {}),
                "ms": ms,
                "ok": succeeded,
                "error": payload.get("error") if not succeeded else None,
            }
        )
        if payload.get("pending_action_id"):
            out.proposed_action_id = payload["pending_action_id"]
        parts.append(
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response=_serialisable(payload)
                )
            )
        )
    return parts


def _serialisable(payload: dict[str, Any]) -> dict[str, Any]:
    """FunctionResponse.response must be a JSON-ish dict. ObjectIds and datetimes are not."""
    import json

    return json.loads(json.dumps(payload, default=str))


def _add_tokens(out: TurnResult, result: provider.Result) -> None:
    for key, value in (result.tokens or {}).items():
        out.tokens[key] = out.tokens.get(key, 0) + value
