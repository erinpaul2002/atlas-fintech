from types import SimpleNamespace

import pytest

from atlas.agent.tools import visuals as visual_tool
from atlas.agent.tools.registry import REGISTRY, ToolContext
from atlas.api import visuals as visuals_api
from atlas.db import visuals as visuals_repo
from atlas.db.client import INDEXES
from atlas.db.models import User
from atlas.visuals.render import CONTENT_SECURITY_POLICY, render_visual_html


def chart_args(**overrides):
    args = {
        "kind": "line",
        "title": "TSLA vs AAPL · 1 year",
        "subtitle": "Indexed performance across the last twelve months.",
        "labels": ["Jan", "Feb", "Mar"],
        "series": [
            {"name": "TSLA", "values": [100, 112.5, 104]},
            {"name": "AAPL", "values": [100, 106, 109]},
        ],
        "value_suffix": "%",
        "takeaway": "AAPL finished ahead with a steadier path.",
        "source_note": "Yahoo Finance · 10 Aug 2026 18:00 UTC",
    }
    args.update(overrides)
    return args


def test_visual_renderer_escapes_content_and_embedded_json():
    spec = visual_tool.normalize_visual(
        chart_args(
            title="TSLA </title><script>alert(1)</script>",
            source_note="Yahoo </script><script>alert(2)</script>",
        )
    )
    output = render_visual_html(spec)

    assert "TSLA &lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "</script><script>alert(2)</script>" not in output
    assert "\\u003c/script\\u003e" in output
    assert "new ResizeObserver" in output
    assert "View accessible data table" in output


def test_visual_csp_allows_only_hashed_executable_script():
    script_policy = CONTENT_SECURITY_POLICY.split(";", 2)[1]
    assert "script-src 'sha256-" in script_policy
    assert "unsafe-inline" not in script_policy
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY


def test_visual_validation_bounds_data_and_disallows_raw_html():
    assert "custom_html" not in REGISTRY["render_visual"].parameters["properties"]
    with pytest.raises(ValueError, match="one value per label"):
        visual_tool.normalize_visual(
            chart_args(series=[{"name": "TSLA", "values": [100]}])
        )
    with pytest.raises(ValueError, match="finite numbers"):
        visual_tool.normalize_visual(
            chart_args(series=[{"name": "TSLA", "values": [100, float("inf"), 104]}])
        )


def test_table_visual_renders_semantic_safe_table():
    spec = visual_tool.normalize_visual(
        {
            "kind": "table",
            "title": "Earnings scorecard",
            "columns": ["Company", "Growth"],
            "rows": [["Nvidia <leader>", 12.4], ["AMD", -2.1]],
            "source_note": "Company filings · Q2 2026",
        }
    )
    output = render_visual_html(spec)

    assert "<table" in output
    assert "Nvidia &lt;leader&gt;" in output
    assert 'class="numeric positive"' in output
    assert 'class="numeric negative"' in output
    assert "id=\"chart\"" not in output


@pytest.mark.asyncio
async def test_render_visual_saves_and_returns_public_link(monkeypatch):
    captured = {}

    async def save(user_id, title, kind, html_content, content_security_policy):
        captured.update(
            user_id=user_id,
            title=title,
            kind=kind,
            html=html_content,
            csp=content_security_policy,
        )
        return "v_abcdefghijklmnop"

    monkeypatch.setattr(visual_tool.visuals_repo, "save_visual", save)
    monkeypatch.setattr(visual_tool.settings, "public_base_url", "https://atlas.example")
    result = await visual_tool.render_visual(
        ToolContext(user=User(id="user-1")), chart_args()
    )

    assert result["data"]["url"] == "https://atlas.example/v/v_abcdefghijklmnop"
    assert result["data"]["expires_in_days"] == 30
    assert captured["user_id"] == "user-1"
    assert "TSLA vs AAPL" in captured["html"]
    assert captured["csp"] == CONTENT_SECURITY_POLICY


@pytest.mark.asyncio
async def test_visual_repository_stores_expiring_document(monkeypatch):
    inserted = {}

    class Collection:
        async def insert_one(self, document):
            inserted.update(document)
            return SimpleNamespace(inserted_id=document["_id"])

    monkeypatch.setattr(
        visuals_repo,
        "db",
        lambda: SimpleNamespace(visuals=Collection()),
    )
    visual_id = await visuals_repo.save_visual(
        "user-1", "Title", "line", "<html>safe</html>", "script-src 'none'"
    )

    assert visual_id.startswith("v_") and len(visual_id) == 18
    assert inserted["views"] == 0
    assert inserted["created_at"].tzinfo is not None
    assert inserted["html_content"] == "<html>safe</html>"
    assert inserted["content_security_policy"] == "script-src 'none'"


@pytest.mark.asyncio
async def test_visual_endpoint_serves_secure_html_and_counts_missing(monkeypatch):
    async def found(visual_id):
        return {
            "html_content": "<!doctype html><title>Atlas chart</title>",
            "content_security_policy": "script-src 'sha256-stored'",
        }

    monkeypatch.setattr(visuals_api.visuals, "get_visual", found)
    response = await visuals_api.view_visual("v_abcdefghijklmnop")

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "script-src 'sha256-stored'"
    assert response.headers["x-robots-tag"].startswith("noindex")
    assert response.headers["x-frame-options"] == "DENY"

    missing = await visuals_api.view_visual("../../not-valid")
    assert missing.status_code == 404
    assert "visual is unavailable" in missing.body.decode().lower()


def test_visual_tool_route_and_ttl_are_registered():
    from atlas.__main__ import app

    assert "render_visual" in REGISTRY
    assert any(
        getattr(route, "original_router", None) is visuals_api.router for route in app.routes
    )
    assert any(route.path == "/v/{visual_id}" for route in visuals_api.router.routes)
    assert (
        "visuals",
        [("created_at", 1)],
        {"expireAfterSeconds": 2592000},
    ) in INDEXES
