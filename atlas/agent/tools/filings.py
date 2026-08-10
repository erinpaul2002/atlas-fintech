"""SEC filings. One tool, two modes — the list, and the answer from inside one filing.

"Anything new in Meta's filings" and "what did it say" are one user intent split across two
turns, so they're one tool (AGENT_SPEC.md §3).
"""

import logging
from typing import Any

from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.llm import provider
from atlas.services import edgar

log = logging.getLogger(__name__)

# a 10-Q runs ~1MB of HTML. Trimmed hard: latency and token burn, not accuracy, are the
# constraint here (ARCHITECTURE.md §9)
READ_CHARS = 180_000


@tool(
    "filings",
    """SEC filings for a company. Without an accession number: the recent filing list (form,
    date, title). With an accession number and a question: reads that filing and answers the
    question from it. Use for "any recent filings", "what's in the latest 10-Q", risk factors,
    segment detail, anything a company said officially.""",
    {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker symbol"},
            "forms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by form type, e.g. ['10-K', '10-Q', '8-K']",
            },
            "accession": {"type": "string", "description": "Accession number of one filing, to read it"},
            "question": {"type": "string", "description": "What to answer from that filing"},
            "limit": {"type": "integer", "description": "How many filings to list. Default 10."},
        },
        "required": ["symbol"],
    },
)
async def filings(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    symbol = str(args.get("symbol", "")).upper().strip()
    if not symbol:
        return {"error": "no symbol given"}
    forms = [str(f).upper() for f in (args.get("forms") or [])] or None
    limit = int(args.get("limit") or 10)

    listing = await edgar.recent_filings(symbol, forms=forms, limit=max(limit, 10))
    if not isinstance(listing, list):
        return listing

    accession = args.get("accession")
    if not accession:
        return ok({"symbol": symbol, "filings": listing[:limit]}, source="SEC EDGAR")

    match = next((f for f in listing if f["accession"] == accession), None)
    if match is None:
        return {"error": f"no filing {accession} found for {symbol}"}

    text = await edgar.filing_text(match["url"], max_chars=READ_CHARS)
    if not isinstance(text, str):
        return text

    question = args.get("question") or "Summarise what matters in this filing."
    answer = await provider.generate(
        system=(
            "You read SEC filings for an analyst. Answer only from the filing text given. "
            "Quote exact figures with their period. If the filing doesn't cover it, say so."
        ),
        prompt=f"Filing: {match['form']} filed {match['filed_at']} by {match['company']}.\n\n"
        f"Question: {question}\n\n---\n{text}",
        temperature=0.2,
    )
    if not answer:
        return {"error": "couldn't read that filing just now"}
    return ok(
        {"symbol": symbol, "filing": match, "answer": answer},
        source=f"SEC EDGAR {match['form']} filed {match['filed_at']}",
        as_of=match["filed_at"],
    )
