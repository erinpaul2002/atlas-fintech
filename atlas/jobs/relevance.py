"""Pure relevance helpers shared by proactive jobs."""

from typing import Any

MIN_SCORE = 4.0


def price_score(change_pct: float | int | None, on_watchlist: bool = True) -> float:
    magnitude = min(abs(float(change_pct or 0)), 8.0)
    return magnitude + (2.0 if on_watchlist else 0.5)


def filing_score(form: str) -> float:
    return {"10-K": 7.0, "10-Q": 6.5, "8-K": 6.0, "6-K": 5.5}.get(form.upper(), 3.0)


def news_score(item: dict[str, Any]) -> float:
    text = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
    material = (
        "earnings", "guidance", "acquire", "merger", "sec", "investigation", "approval",
        "contract", "offering", "dividend", "buyback", "ceo", "cfo", "bankruptcy",
    )
    return 6.0 if any(term in text for term in material) else 4.0


def keep(candidates: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return sorted(
        (candidate for candidate in candidates if float(candidate.get("score") or 0) >= MIN_SCORE),
        key=lambda candidate: float(candidate.get("score") or 0),
        reverse=True,
    )[:limit]
