"""Web search — Gemini's Google Search grounding, no provider client, no key.

Gemini rejects the grounding tool alongside function declarations (verified live, 400
INVALID_ARGUMENT), so this declared function issues its own grounding-only call. Same
behaviour to the user, one extra round trip on a minority of turns.
"""

from typing import Any

from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.llm import provider


@tool(
    "web_search",
    """Search the web for things the finance APIs don't cover: private companies, funding
    rounds, M&A rumours, macro and policy, industry context, people. Never for prices or
    fundamentals of a listed company — those come from get_quote and research_company.""",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to search for"}},
        "required": ["query"],
    },
)
async def web_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "no query given"}
    result = await provider.ground(query)
    if "error" in result:
        return result
    return ok(result, source="Google Search")
