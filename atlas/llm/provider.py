"""Gemini adapter + text-only fallback.

The canonical conversation format is `types.Content` — the loop builds and mutates those
directly, because a neutral message type would be a translation layer we'd debug instead of
the thing Gemini actually sees (ARCHITECTURE.md §1.3). The fallback converts on its way out.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from google.genai import Client, types

from atlas.config import settings
from atlas.llm.transcription import transcribe_audio

log = logging.getLogger(__name__)

_client = Client(api_key=settings.gemini_api_key)

GEMINI_TIMEOUT_MS = 45_000
ATTEMPTS = 2

OnDelta = Callable[[str], Awaitable[None]] | None


@dataclass
class Result:
    text: str = ""
    function_calls: list[types.FunctionCall] = field(default_factory=list)
    # The original function-call Parts, kept whole. 2.5 is a thinking model and stamps each one
    # with a `thought_signature`; Google's contract is that it comes back unmodified on the next
    # request. Rebuilding a Part from just the FunctionCall drops it — works today, but it is a
    # documented 400 waiting for the model to start thinking on a turn that matters.
    call_parts: list[types.Part] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    provider: str = "gemini"
    error: str | None = None


def _config(system: str, tools: list[types.Tool] | None, temperature: float = 0.4) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system or None,
        tools=tools or None,
        temperature=temperature,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _out_of_quota(exc: Exception) -> bool:
    """A 429 on the free tier is a daily cap (20/day on gemini-2.5-flash), not a blip."""
    return "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)[:20]


def has_media(contents: list[types.Content]) -> bool:
    return any(
        p.inline_data is not None
        for c in contents
        for p in (c.parts or [])
    )


def _gemini_models() -> list[str]:
    """Configured Gemini tiers, in order, without blank or duplicate model IDs."""
    configured = (settings.gemini_model.strip(), settings.gemini_fallback_model.strip())
    return list(dict.fromkeys(filter(None, configured)))


async def stream(
    system: str,
    contents: list[types.Content],
    tools: list[types.Tool] | None = None,
    on_delta: OnDelta = None,
    temperature: float = 0.4,
) -> Result:
    """One model turn. Streams text deltas through `on_delta`, returns text + any function calls.

    Each Gemini tier gets up to two attempts. If every tier fails, voice notes are transcribed
    before the text fallback; other media cannot use that final tier.
    """
    last_exc: Exception | None = None
    for model_index, model in enumerate(_gemini_models()):
        if model_index:
            log.warning("failing over to Gemini model %s", model)
        for attempt in range(ATTEMPTS):
            try:
                return await _stream_gemini(
                    system, contents, tools, on_delta, temperature, model=model
                )
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "gemini model %s attempt %d failed: %s",
                    model, attempt + 1, str(exc)[:200],
                )
                if _out_of_quota(exc):
                    break
                if attempt < ATTEMPTS - 1:
                    await asyncio.sleep(0.6 * (attempt + 1))

    if has_media(contents):
        try:
            contents = await transcribe_audio(contents)
        except Exception as exc:
            log.error("all Gemini models and voice transcription failed: %s", exc)
            return Result(text="", provider="gemini", error=str(exc))
        if has_media(contents):
            log.error("all Gemini models failed on a non-audio media turn: %s", last_exc)
            return Result(text="", provider="gemini", error=str(last_exc))

    try:
        return await _fallback(system, contents, on_delta)
    except Exception as exc:
        log.error("fallback failed too: %s", exc)
        return Result(text="", provider="fallback", error=str(exc))


async def _stream_gemini(
    system: str,
    contents: list[types.Content],
    tools: list[types.Tool] | None,
    on_delta: OnDelta,
    temperature: float,
    model: str | None = None,
) -> Result:
    out = Result()
    iterator = await _client.aio.models.generate_content_stream(
        model=model or settings.gemini_model,
        contents=contents,
        config=_config(system, tools, temperature),
    )
    async for chunk in iterator:
        for part in _parts(chunk):
            if part.function_call:
                out.function_calls.append(part.function_call)
                out.call_parts.append(part)
            elif part.text:
                out.text += part.text
                if on_delta:
                    await on_delta(out.text)
        if chunk.usage_metadata:
            out.tokens = {
                "input": chunk.usage_metadata.prompt_token_count or 0,
                "output": chunk.usage_metadata.candidates_token_count or 0,
            }
    return out


def _parts(chunk: Any) -> list[types.Part]:
    if not chunk.candidates:
        return []
    content = chunk.candidates[0].content
    return list(content.parts or []) if content else []


async def _generate_with_failover(
    contents: Any, config: types.GenerateContentConfig, operation: str
) -> tuple[Any | None, Exception | None]:
    last_exc: Exception | None = None
    for model in _gemini_models():
        for attempt in range(ATTEMPTS):
            try:
                response = await _client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return response, None
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "%s model %s attempt %d failed: %s",
                    operation, model, attempt + 1, str(exc)[:200],
                )
                if _out_of_quota(exc):
                    break
                if attempt < ATTEMPTS - 1:
                    await asyncio.sleep(0.6 * (attempt + 1))
    return None, last_exc


async def generate(system: str, prompt: str, temperature: float = 0.4, json_schema: dict | None = None) -> str:
    """Non-streaming single-shot — digests, fact extraction, the narrative, brief passes."""
    config = _config(system, None, temperature)
    if json_schema:
        config.response_mime_type = "application/json"
        config.response_json_schema = json_schema
    response, _ = await _generate_with_failover(prompt, config, "generate")
    return response.text or "" if response else ""


async def ground(query: str) -> dict[str, Any]:
    """Google Search grounding as its own call.

    Verified live 6 Aug 2026: Gemini rejects google_search alongside function declarations
    ("Built-in tools and Function Calling cannot be combined"), so web_search is a declared
    function whose implementation issues this second, grounding-only request —
    ARCHITECTURE.md §1.1 open item 2, resolved.
    """
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.2,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    r, exc = await _generate_with_failover(query, config, "ground")
    if not r:
        return {"error": f"web search failed: {str(exc)[:150]}"}

    sources: list[dict[str, str]] = []
    meta = r.candidates[0].grounding_metadata if r.candidates else None
    for c in (meta.grounding_chunks if meta and meta.grounding_chunks else []):
        if c.web and c.web.uri:
            sources.append({"title": c.web.title or "", "url": c.web.uri})
    return {"summary": r.text or "", "sources": sources[:5]}


async def embed(texts: list[str]) -> list[list[float]]:
    try:
        r = await _client.aio.models.embed_content(
            model=settings.embed_model,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=768),
        )
        return [list(e.values or []) for e in (r.embeddings or [])]
    except Exception as exc:
        log.warning("embed failed: %s", exc)
        return []


# --- fallback: text-only, no tools, no media -----------------------------------------------

FALLBACK_URL = settings.fallback_url

# The fallback has no tools. Left to itself it will happily invent a price and a source —
# observed, and a hard-rule violation. Tool results already fetched this turn are handed to it
# as text; beyond those it is not allowed to state a figure at all.
FALLBACK_WITH_DATA = (
    "\n\nThis turn you have no tools. Every figure you may use is in the tool results above; "
    "quote those and nothing else. Do not add a price, date, headline or source that is not there."
)
FALLBACK_NO_DATA = (
    "\n\nThis turn you have no tools and no live data at all. Do not state any price, figure, "
    "date, headline or source — you would be inventing it. Say in one line that you can't reach "
    "live market data this minute and ask them to try again shortly."
)


def _to_messages(system: str, contents: list[types.Content]) -> list[dict[str, str]]:
    """types.Content → OpenAI-shaped messages, carrying tool results across as text."""
    messages: list[dict[str, str]] = []
    saw_tool_data = False
    for c in contents:
        chunks = []
        for p in c.parts or []:
            if p.text:
                chunks.append(p.text)
            elif p.function_response:
                saw_tool_data = True
                chunks.append(f"[data from {p.function_response.name}: {p.function_response.response}]")
            elif p.function_call:
                chunks.append(f"[looked up {p.function_call.name}]")
        text = "\n".join(chunks)
        if text.strip():
            messages.append({"role": "assistant" if c.role == "model" else "user", "content": text})

    guard = FALLBACK_WITH_DATA if saw_tool_data else FALLBACK_NO_DATA
    messages.insert(0, {"role": "system", "content": (system or "") + guard})
    if messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "(continue)"})
    return messages


async def _fallback(system: str, contents: list[types.Content], on_delta: OnDelta) -> Result:
    messages = _to_messages(system, contents)
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            FALLBACK_URL,
            headers={"Authorization": f"Bearer {settings.fallback_llm_api_key}"},
            json={"model": settings.fallback_model, "messages": messages, "temperature": 0.4},
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]

    log.warning("served this turn from the fallback provider")
    if on_delta:
        await on_delta(text)
    return Result(text=text, provider="fallback")


async def warmup() -> None:
    try:
        await generate("", "ok", temperature=0)
    except Exception:
        pass
