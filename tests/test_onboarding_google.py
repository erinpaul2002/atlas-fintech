from types import SimpleNamespace

import pytest

from atlas.agent import actions
from atlas.agent.actions import proposal_summary, target_start
from atlas.agent.background import OFFER_PATTERNS
from atlas.agent.tools.google import propose_sheet_write, read_sheet
from atlas.agent.tools.registry import ToolContext
from atlas.agent.tools.registry import REGISTRY
from atlas.bot import onboarding
from atlas.bot.onboarding import FIRST_GREETING, QUESTIONS
from atlas.bot.inbound import Inbound, normalize
from atlas.db.models import PendingAction, User, utcnow
from atlas.services.google import parse_spreadsheet, sheet_result


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def test_onboarding_gap_is_never_offered_twice():
    user = User(onboarding_offered=["role"])
    assert "role" not in user.unknowns()
    assert "interests" in user.unknowns()


def test_filled_onboarding_gaps_disappear_without_a_stage():
    user = User(role="portfolio manager", interests={"sectors": ["semiconductors"]})
    assert "role" not in user.unknowns()
    assert "interests" not in user.unknowns()
    assert "when the morning brief should land" in user.unknowns()


def test_first_onboarding_question_is_recognized_as_offered():
    assert OFFER_PATTERNS["role"].search(QUESTIONS["role"])


def test_first_greeting_identifies_atlas_and_asks_one_question():
    greeting = FIRST_GREETING.format(name=" Erin")
    assert "I'm Atlas" in greeting
    assert greeting.count("?") == 1
    assert "Google" in greeting
    assert "skip onboarding" in greeting


@pytest.mark.asyncio
async def test_start_is_normalized_as_hello_without_losing_the_start_signal():
    message = SimpleNamespace(
        text="/start", caption=None, voice=None, audio=None, photo=None, document=None
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=42),
        effective_user=SimpleNamespace(id=7, first_name="Erin"),
    )

    inbound = await normalize(update, SimpleNamespace())

    assert inbound is not None
    assert inbound.text == "Hi"
    assert inbound.is_start is True


@pytest.mark.asyncio
async def test_start_begins_resumable_onboarding_even_when_history_exists(monkeypatch):
    transitions = []

    async def transition(user_id, status, step):
        transitions.append((status, step))

    monkeypatch.setattr(onboarding.users_repo, "set_onboarding", transition)
    inbound = Inbound(
        chat_id=42, telegram_user_id=7, first_name="Erin", text="Hi", is_start=True
    )

    reply = await onboarding.handle(
        User(first_name="Erin"), inbound, first_turn=False, google_connected=False
    )

    assert "set things up now" in reply
    assert transitions == [("in_progress", "choice")]


@pytest.mark.asyncio
async def test_setup_choice_advances_one_question_or_skips(monkeypatch):
    transitions = []

    async def transition(user_id, status, step):
        transitions.append((status, step))

    monkeypatch.setattr(onboarding.users_repo, "set_onboarding", transition)
    user = User(onboarding_status="in_progress", onboarding_step="choice")

    for phrase in ("setup", "setup now", "yes, set me up"):
        now = Inbound(chat_id=42, telegram_user_id=7, first_name="Erin", text=phrase)
        assert await onboarding.handle(
            user, now, first_turn=False, google_connected=False
        ) == QUESTIONS["role"]
        assert transitions[-1] == ("in_progress", "role")

    skip = Inbound(chat_id=42, telegram_user_id=7, first_name="Erin", text="skip onboarding")
    assert "onboarding skipped" in (
        await onboarding.handle(user, skip, first_turn=False, google_connected=False)
    )
    assert transitions[-1] == ("skipped", "done")

    mid_setup = User(onboarding_status="in_progress", onboarding_step="interests")
    plain_skip = Inbound(chat_id=42, telegram_user_id=7, first_name="Erin", text="skip")
    assert "onboarding skipped" in (
        await onboarding.handle(
            mid_setup, plain_skip, first_turn=False, google_connected=False
        )
    )


@pytest.mark.asyncio
async def test_google_setup_uses_one_clickable_link_for_all_services(monkeypatch):
    transitions = []

    async def transition(user_id, status, step):
        transitions.append((status, step))

    async def link(ctx):
        return "https://atlas.example/oauth/google/start?state=one_time"

    monkeypatch.setattr(onboarding.users_repo, "set_onboarding", transition)
    monkeypatch.setattr(onboarding, "connection_link", link)
    user = User(onboarding_status="in_progress", onboarding_step="google")
    inbound = Inbound(chat_id=42, telegram_user_id=7, first_name="Erin", text="connect now")

    reply = await onboarding.handle(
        user, inbound, first_turn=False, google_connected=False
    )

    assert "[Tap here to connect Google](https://atlas.example/oauth/google/start?state=one_time)" in reply
    assert "open this address" in reply
    assert reply.count("https://atlas.example/oauth/google/start?state=one_time") == 2
    assert transitions == [("in_progress", "google_pending")]


@pytest.mark.asyncio
async def test_missing_link_report_resends_the_link(monkeypatch):
    async def transition(user_id, status, step):
        return None

    async def link(ctx):
        return "https://atlas.example/oauth/google/start?state=fresh"

    monkeypatch.setattr(onboarding.users_repo, "set_onboarding", transition)
    monkeypatch.setattr(onboarding, "connection_link", link)
    user = User(onboarding_status="in_progress", onboarding_step="google_pending")
    inbound = Inbound(
        chat_id=42, telegram_user_id=7, first_name="Erin", text="I don't see any link"
    )

    reply = await onboarding.handle(
        user, inbound, first_turn=False, google_connected=False
    )

    assert "state=fresh" in reply


@pytest.mark.asyncio
async def test_idk_skips_the_current_profile_question_without_storing_it(monkeypatch):
    transitions = []
    offered = []

    async def transition(user_id, status, step):
        transitions.append((status, step))

    async def mark(user_id, gaps):
        offered.extend(gaps)

    async def forbidden_patch(*args, **kwargs):
        raise AssertionError("idk must not be stored as a profile value")

    monkeypatch.setattr(onboarding.users_repo, "set_onboarding", transition)
    monkeypatch.setattr(onboarding.users_repo, "mark_onboarding_offered", mark)
    monkeypatch.setattr(onboarding.users_repo, "patch", forbidden_patch)
    user = User(onboarding_status="in_progress", onboarding_step="intel_preferences")
    inbound = Inbound(chat_id=42, telegram_user_id=7, first_name="Erin", text="idk")

    reply = await onboarding.handle(
        user, inbound, first_turn=False, google_connected=False
    )

    assert reply == QUESTIONS["brief_time"]
    assert offered == ["what's worth interrupting them for"]
    assert transitions == [("in_progress", "brief_time")]


def test_brief_time_requires_a_timezone_and_parses_ist():
    assert onboarding._brief_time("8:30 am", "UTC") is None
    assert onboarding._brief_time("8:30 am IST", "UTC") == (8, 30, "Asia/Kolkata")


def test_media_is_attached_to_the_native_model_request():
    from atlas.agent.context import current_turn
    from atlas.llm.provider import has_media

    turn = current_turn("What matters in this chart?", [("image/png", b"image-bytes")])

    assert has_media([turn])
    assert turn.parts[0].inline_data.mime_type == "image/png"


def test_live_finance_capabilities_are_tool_backed():
    assert {
        "get_quote",
        "market_snapshot",
        "research_company",
        "compare_companies",
        "filings",
    } <= set(REGISTRY)


def test_sheet_url_parses_tab_gid_from_fragment():
    parsed = parse_spreadsheet(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=2718")
    assert parsed == (SHEET_ID, "2718")
    assert parse_spreadsheet(SHEET_ID) == (SHEET_ID, None)


def test_large_sheet_is_summarized_with_numeric_stats():
    rows = [["symbol", "price"]] + [[f"T{i}", str(i)] for i in range(101)]
    result = sheet_result(rows)
    assert "rows" not in result
    assert result["row_count"] == 102
    assert result["numeric_stats"]["price"]["max"] == 100


def test_write_summary_names_destructive_range_and_row_count():
    summary = proposal_summary({"mode": "update_cells", "target": "Tracker!B4", "values": [["a"], ["b"]]})
    assert "2 rows" in summary
    assert "Tracker!B4" in summary
    assert target_start("Tracker!B4:D8") == ("Tracker!B", 4)


def test_model_can_propose_but_has_no_execute_tool():
    assert "propose_sheet_write" in REGISTRY
    assert all("execute" not in name and "confirm" not in name for name in REGISTRY)


def test_google_oauth_routes_are_registered():
    from atlas.__main__ import app

    paths = set(app.openapi()["paths"])
    assert {"/oauth/google/start", "/oauth/google/callback"} <= paths


def test_google_tokens_are_encrypted_at_rest():
    from atlas.db.integrations import _decrypt, _encrypt

    encrypted = _encrypt("access-token")
    assert encrypted != b"access-token"
    assert _decrypt(encrypted) == "access-token"


@pytest.mark.asyncio
async def test_confirmation_uses_conservative_model_classification(monkeypatch):
    async def classify(**kwargs):
        return '{"decision":"confirm"}'

    monkeypatch.setattr(actions.provider, "generate", classify)
    assert await actions.classify_reply("yeah, do it", "Append 2 rows") == "confirm"

    async def malformed(**kwargs):
        return "not-json"

    monkeypatch.setattr(actions.provider, "generate", malformed)
    assert await actions.classify_reply("maybe", "Append 2 rows") == "other"


@pytest.mark.asyncio
async def test_public_sheet_read_does_not_require_oauth(monkeypatch):
    from atlas.agent.tools import google as tool_module

    async def public(*args, **kwargs):
        return [["symbol", "price"], ["NVDA", "180"]]

    async def should_not_auth(*args, **kwargs):
        raise AssertionError("public sheet unexpectedly requested OAuth")

    monkeypatch.setattr(tool_module.google, "public_sheet", public)
    monkeypatch.setattr(tool_module.integrations, "google_access_token", should_not_auth)
    result = await read_sheet(ToolContext(user=User(id="u")), {"url_or_id": SHEET_ID})
    assert result["data"]["rows"][1] == ["NVDA", "180"]


@pytest.mark.asyncio
async def test_sheet_proposal_never_executes_the_write(monkeypatch):
    from atlas.agent.tools import google as tool_module

    async def token(*args, **kwargs):
        return "token"

    async def titles(*args, **kwargs):
        return ["Existing"]

    async def proposed(user_id, spec):
        return PendingAction(
            id="pending-1", user_id=user_id, spec=spec, summary="Add 1 row", status="pending", proposed_at=utcnow()
        )

    async def forbidden_write(*args, **kwargs):
        raise AssertionError("a proposal executed a Google write")

    monkeypatch.setattr(tool_module.integrations, "google_access_token", token)
    monkeypatch.setattr(tool_module.google, "spreadsheet_titles", titles)
    monkeypatch.setattr(tool_module.actions, "propose", proposed)
    monkeypatch.setattr(tool_module.google, "update_values", forbidden_write)
    result = await propose_sheet_write(
        ToolContext(user=User(id="u")),
        {"url_or_id": SHEET_ID, "mode": "new_tab", "target": "New", "values": [["NVDA"]]},
    )
    assert result["pending_action_id"] == "pending-1"
