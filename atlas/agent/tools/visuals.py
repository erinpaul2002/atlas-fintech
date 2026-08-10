"""Create short-lived interactive charts and tables from structured, sourced data."""

import math
import re
from typing import Any

from atlas.agent.tools.registry import ToolContext, ok, tool
from atlas.config import settings
from atlas.db import visuals as visuals_repo
from atlas.visuals.render import CONTENT_SECURITY_POLICY, render_visual_html

CHART_KINDS = {"line", "area", "bar", "doughnut"}
ALL_KINDS = CHART_KINDS | {"table"}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@tool(
    "render_visual",
    """Create a polished interactive chart or data table and return a public HTTPS link.
    Use when the user asks for a chart, graph, visual comparison, allocation view, or dashboard-like
    presentation. Only visualize figures returned by data tools in this turn; never invent points.
    Prefer line/area for trends, bar for comparisons, doughnut for composition, and table for dense data.""",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(ALL_KINDS)},
            "title": {"type": "string", "description": "Short, specific visual title."},
            "subtitle": {"type": "string", "description": "One sentence describing scope and period."},
            "labels": {"type": "array", "items": {"type": "string"}},
            "series": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "values": {"type": "array", "items": {"type": "number", "nullable": True}},
                        "color": {"type": "string", "description": "Optional #RRGGBB color."},
                    },
                    "required": ["name", "values"],
                },
            },
            "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {
                "type": "array",
                "items": {"type": "array", "items": {}},
                "description": "Table rows; each row must match columns.",
            },
            "value_prefix": {"type": "string", "description": "For example $ or ₹."},
            "value_suffix": {"type": "string", "description": "For example % or x."},
            "takeaway": {"type": "string", "description": "One decision-useful observation."},
            "source_note": {"type": "string", "description": "Data provider and as-of time/date."},
        },
        "required": ["kind", "title", "source_note"],
    },
)
async def render_visual(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = normalize_visual(args)
    except ValueError as exc:
        return {"error": str(exc)}
    if ctx.user.id is None:
        return {"error": "visuals require a saved Atlas user"}

    try:
        public_base_url = settings.effective_public_base_url
    except ValueError:
        return {"error": "visual links are not configured on this deployment"}
    html_content = render_visual_html(spec)
    visual_id = await visuals_repo.save_visual(
        ctx.user.id,
        spec["title"],
        spec["kind"],
        html_content,
        CONTENT_SECURITY_POLICY,
    )
    url = f"{public_base_url}/v/{visual_id}"
    return ok(
        {
            "visual_id": visual_id,
            "url": url,
            "title": spec["title"],
            "kind": spec["kind"],
            "link_markdown": f"[View interactive visual]({url})",
            "expires_in_days": 30,
        },
        source="Atlas visual renderer",
    )


def normalize_visual(args: dict[str, Any]) -> dict[str, Any]:
    kind = _text(args.get("kind"), "kind", 20).lower()
    if kind not in ALL_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(ALL_KINDS))}")
    spec: dict[str, Any] = {
        "kind": kind,
        "title": _text(args.get("title"), "title", 120),
        "subtitle": _optional_text(args.get("subtitle"), 240),
        "takeaway": _optional_text(args.get("takeaway"), 280),
        "source_note": _text(args.get("source_note"), "source_note", 180),
        "value_prefix": _optional_text(args.get("value_prefix"), 8),
        "value_suffix": _optional_text(args.get("value_suffix"), 8),
    }
    if kind == "table":
        spec.update(_normalize_table(args))
    else:
        spec.update(_normalize_chart(args, doughnut=kind == "doughnut"))
    return spec


def _normalize_chart(args: dict[str, Any], doughnut: bool) -> dict[str, Any]:
    raw_labels = args.get("labels")
    raw_series = args.get("series")
    if not isinstance(raw_labels, list) or not 2 <= len(raw_labels) <= 80:
        raise ValueError("charts require between 2 and 80 labels")
    if not isinstance(raw_series, list) or not 1 <= len(raw_series) <= 6:
        raise ValueError("charts require between 1 and 6 series")
    if doughnut and len(raw_series) != 1:
        raise ValueError("doughnut charts require exactly one series")

    labels = [_text(value, "label", 48) for value in raw_labels]
    series: list[dict[str, Any]] = []
    for raw in raw_series:
        if not isinstance(raw, dict):
            raise ValueError("each series must be an object")
        values = raw.get("values")
        if not isinstance(values, list) or len(values) != len(labels):
            raise ValueError("each series must have one value per label")
        clean_values = [_number(value) for value in values]
        if doughnut and any(value is not None and value < 0 for value in clean_values):
            raise ValueError("doughnut values cannot be negative")
        item: dict[str, Any] = {
            "name": _text(raw.get("name"), "series name", 60),
            "values": clean_values,
        }
        color = _optional_text(raw.get("color"), 7)
        if color:
            if not HEX_COLOR.fullmatch(color):
                raise ValueError("series colors must use #RRGGBB")
            item["color"] = color
        series.append(item)
    if not any(value is not None for item in series for value in item["values"]):
        raise ValueError("charts require at least one numeric value")
    if doughnut and sum(value or 0 for value in series[0]["values"]) <= 0:
        raise ValueError("doughnut charts require a positive total")
    return {"labels": labels, "series": series}


def _normalize_table(args: dict[str, Any]) -> dict[str, Any]:
    raw_columns = args.get("columns")
    raw_rows = args.get("rows")
    if not isinstance(raw_columns, list) or not 1 <= len(raw_columns) <= 12:
        raise ValueError("tables require between 1 and 12 columns")
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= 80:
        raise ValueError("tables require between 1 and 80 rows")
    columns = [_text(value, "column", 48) for value in raw_columns]
    rows: list[list[Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, list) or len(raw_row) != len(columns):
            raise ValueError("every table row must match the column count")
        rows.append([_table_value(value) for value in raw_row])
    return {"columns": columns, "rows": rows}


def _number(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("chart values must be finite numbers or null")
    return value


def _table_value(value: Any) -> str | float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("table numbers must be finite")
        return value
    return _optional_text(value, 120)


def _text(value: Any, name: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > limit:
        raise ValueError(f"{name} must be {limit} characters or fewer")
    return text


def _optional_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise ValueError(f"text must be {limit} characters or fewer")
    return text
