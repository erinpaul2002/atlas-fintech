"""SEC EDGAR — filings. `SEC_USER_AGENT` is mandatory: 403 without it, 200 with it (observed)."""

import logging
import re
from typing import Any

from atlas.config import settings
from atlas.services.util import TTLCache, fetch_json, http

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_cik_map: dict[str, tuple[str, str]] = {}          # SYMBOL -> (cik10, company name)
_submissions = TTLCache(ttl=600)


async def cik_for(symbol: str) -> tuple[str, str] | None:
    global _cik_map
    symbol = symbol.upper().strip()
    if not _cik_map:
        raw = await fetch_json(TICKERS_URL, headers=HEADERS, what="EDGAR ticker map")
        if isinstance(raw, dict) and "error" not in raw:
            _cik_map = {
                str(v["ticker"]).upper(): (str(v["cik_str"]).zfill(10), v.get("title", ""))
                for v in raw.values()
                if v.get("ticker")
            }
    return _cik_map.get(symbol)


async def recent_filings(symbol: str, forms: list[str] | None = None, limit: int = 10) -> Any:
    hit = await cik_for(symbol)
    if not hit:
        return {"error": f"no SEC filer found for {symbol}"}
    cik, company = hit

    cached = _submissions.get(cik)
    if cached is None:
        cached = await fetch_json(
            f"https://data.sec.gov/submissions/CIK{cik}.json", headers=HEADERS, what=f"EDGAR submissions {symbol}"
        )
        if "error" in cached:
            return cached
        _submissions.put(cik, cached)

    recent = cached.get("filings", {}).get("recent", {})
    wanted = {f.upper() for f in forms} if forms else None
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if wanted and form.upper() not in wanted:
            continue
        out.append(
            {
                "form": form,
                "filed_at": recent["filingDate"][i],
                "accession": recent["accessionNumber"][i],
                "title": recent.get("primaryDocDescription", [""] * (i + 1))[i] or form,
                "url": _doc_url(cik, recent["accessionNumber"][i], recent["primaryDocument"][i]),
                "company": company,
                "symbol": symbol.upper(),
            }
        )
        if len(out) >= limit:
            break
    return out or {"error": f"no {'/'.join(forms) if forms else ''} filings found for {symbol}"}


def _doc_url(cik: str, accession: str, primary_doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_doc}"


async def filing_text(url: str, max_chars: int = 400_000) -> Any:
    """Filings arrive as ~1MB of HTML/XBRL. Strip to text; the digest pass does the rest."""
    try:
        r = await http().get(url, headers=HEADERS, timeout=30.0)
        r.raise_for_status()
    except Exception as exc:
        log.warning("EDGAR document fetch failed: %s", exc)
        return {"error": f"couldn't fetch the filing ({type(exc).__name__})"}
    return strip_html(r.text)[:max_chars]


TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
SPACE = re.compile(r"[ \t\xa0]+")
BLANK = re.compile(r"\n{3,}")


def strip_html(html: str) -> str:
    text = TAG.sub(" ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#8217;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    return BLANK.sub("\n\n", SPACE.sub(" ", text)).strip()
