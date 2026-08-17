import base64
from types import SimpleNamespace

import pytest

from atlas.agent import actions
from atlas.agent.tools.google_calendar import propose_calendar_event
from atlas.agent.tools.registry import REGISTRY, ToolContext
from atlas.bot.handlers import _action_reply
from atlas.db import integrations
from atlas.db.models import PendingAction, User, utcnow
from atlas.services import google
from atlas.services import google_calendar as calendar_service
from atlas.services import google_content


class JsonResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


@pytest.mark.asyncio
async def test_create_calendar_event_builds_invite_and_reminder_payload(monkeypatch):
    captured = {}

    class Client:
        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return JsonResponse(
                {
                    "id": "event-1",
                    "summary": "Nvidia earnings",
                    "start": {"dateTime": "2026-08-11T15:00:00+05:30"},
                    "end": {"dateTime": "2026-08-11T16:00:00+05:30"},
                    "attendees": [{"email": "team@example.com"}],
                    "htmlLink": "https://calendar.google.com/event?eid=one",
                    "status": "confirmed",
                }
            )

    monkeypatch.setattr(calendar_service, "http", lambda: Client())
    result = await google.create_calendar_event(
        "token",
        "Nvidia earnings",
        "2026-08-11T15:00:00+05:30",
        "2026-08-11T16:00:00+05:30",
        description="Review the quarter",
        attendees=["team@example.com"],
        location="Conference room",
        timezone="Asia/Kolkata",
        reminder_minutes=[60],
        event_id="abc123",
    )

    assert captured["params"] == {"sendUpdates": "all"}
    assert captured["json"]["id"] == "abc123"
    assert captured["json"]["start"]["timeZone"] == "Asia/Kolkata"
    assert captured["json"]["attendees"] == [{"email": "team@example.com"}]
    assert captured["json"]["reminders"]["overrides"] == [
        {"method": "popup", "minutes": 60}
    ]
    assert result["html_link"].startswith("https://calendar.google.com/")


@pytest.mark.asyncio
async def test_calendar_proposal_requires_confirmation_and_never_creates(monkeypatch):
    from atlas.agent.tools import google_calendar as tool_module

    async def token(*args, **kwargs):
        return "token"

    async def proposed(user_id, spec, kind):
        assert kind == "calendar_event"
        assert spec["reminder_minutes"] == [60]
        return PendingAction(
            id="pending-calendar",
            user_id=user_id,
            kind=kind,
            spec=spec,
            summary="Schedule Nvidia earnings review",
            status="pending",
            proposed_at=utcnow(),
        )

    async def forbidden_create(*args, **kwargs):
        raise AssertionError("calendar proposal executed the event")

    monkeypatch.setattr(tool_module.integrations, "google_access_token", token)
    monkeypatch.setattr(tool_module.actions, "propose", proposed)
    monkeypatch.setattr(google, "create_calendar_event", forbidden_create)
    result = await propose_calendar_event(
        ToolContext(user=User(id="u", timezone="Asia/Kolkata")),
        {
            "summary": "Nvidia earnings review",
            "start": "2026-08-11T15:00:00+05:30",
            "end": "2026-08-11T16:00:00+05:30",
            "attendees": ["team@example.com"],
            "reminder_minutes": [60],
        },
    )

    assert result["pending_action_id"] == "pending-calendar"


@pytest.mark.asyncio
async def test_confirmed_calendar_action_executes_with_deterministic_id(monkeypatch):
    updates = []
    action = PendingAction(
        id="action-1",
        user_id="u",
        kind="calendar_event",
        idempotency_key="sha256:" + "a" * 64,
        spec={
            "summary": "Nvidia earnings review",
            "start": "2026-08-11T15:00:00+05:30",
            "end": "2026-08-11T16:00:00+05:30",
            "timezone": "Asia/Kolkata",
        },
        status="pending",
        proposed_at=utcnow(),
    )
    claimed = action.model_copy(update={"status": "executing"}).model_dump(by_alias=True)

    class PendingActions:
        async def find_one_and_update(self, *args, **kwargs):
            return claimed

        async def update_one(self, *args, **kwargs):
            updates.append((args, kwargs))

    monkeypatch.setattr(actions, "db", lambda: SimpleNamespace(pending_actions=PendingActions()))

    async def token(*args, **kwargs):
        return "calendar-token"

    captured = {}

    async def create(*args, **kwargs):
        captured.update(kwargs)
        return {
            "summary": "Nvidia earnings review",
            "start": {"dateTime": "2026-08-11T15:00:00+05:30"},
            "html_link": "https://calendar.google.com/event?eid=one",
        }

    monkeypatch.setattr(actions.integrations, "google_access_token", token)
    monkeypatch.setattr(actions.google, "create_calendar_event", create)

    result = await actions.confirm_and_execute(User(id="u"), action)

    assert result["ok"] is True
    assert result["kind"] == "calendar_event"
    assert captured["event_id"] == "a" * 48
    assert updates


@pytest.mark.asyncio
async def test_gmail_full_message_decodes_nested_plain_text(monkeypatch):
    encoded = base64.urlsafe_b64encode(b"Q3 targets are 12% growth.\nRegards, Erin").decode().rstrip("=")

    async def get_json(*args, **kwargs):
        return {
            "id": "message-1",
            "threadId": "thread-1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Q3 targets"},
                    {"name": "From", "value": "cfo@example.com"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded}},
                    {
                        "mimeType": "application/pdf",
                        "filename": "targets.pdf",
                        "body": {"attachmentId": "attachment-1"},
                    },
                ],
            },
        }

    monkeypatch.setattr(google_content, "_get_json", get_json)
    result = await google.gmail_get_message_full("token", "message-1")

    assert result["subject"] == "Q3 targets"
    assert "12% growth" in result["body"]
    assert result["attachments"][0]["filename"] == "targets.pdf"
    assert result["body_truncated"] is False


@pytest.mark.asyncio
async def test_gmail_full_message_fetches_large_body_attachment(monkeypatch):
    encoded = base64.urlsafe_b64encode(b"The full long-form email body.").decode().rstrip("=")

    async def get_json(url, *args, **kwargs):
        if "/attachments/" in url:
            return {"data": encoded}
        return {
            "threadId": "thread-1",
            "payload": {
                "headers": [{"name": "Subject", "value": "Long update"}],
                "mimeType": "text/plain",
                "body": {"attachmentId": "body-1"},
            },
        }

    monkeypatch.setattr(google_content, "_get_json", get_json)
    result = await google.gmail_get_message_full("token", "message-2")

    assert result["body"] == "The full long-form email body."


@pytest.mark.asyncio
async def test_drive_google_doc_exports_plain_text(monkeypatch):
    captured = {}

    async def get_bytes(url, token, what, params=None):
        captured.update(url=url, params=params)
        return b"Revenue grew while gross margin compressed."

    monkeypatch.setattr(google_content, "_get_bytes", get_bytes)
    result = await google.drive_read_file(
        "token",
        "file-1",
        google_content.GOOGLE_DOC,
        "Annual report notes",
    )

    assert captured["url"].endswith("/file-1/export")
    assert captured["params"] == {"mimeType": "text/plain"}
    assert "gross margin" in result["text"]
    assert result["name"] == "Annual report notes"


@pytest.mark.asyncio
async def test_drive_pdf_extracts_page_text(monkeypatch):
    async def get_bytes(*args, **kwargs):
        return b"fake-pdf"

    class Page:
        def extract_text(self):
            return "Risk factors include customer concentration."

    monkeypatch.setattr(google_content, "_get_bytes", get_bytes)
    monkeypatch.setattr(
        google_content,
        "PdfReader",
        lambda stream: SimpleNamespace(pages=[Page(), Page()]),
    )
    result = await google.drive_read_file(
        "token", "pdf-1", "application/pdf", "Annual report.pdf"
    )

    assert result["page_count"] == 2
    assert "customer concentration" in result["text"]


def test_new_google_tools_are_registered_without_execute_tools():
    assert {"propose_calendar_event", "read_drive_file", "get_email_detail"} <= set(REGISTRY)
    assert all("execute" not in name and "confirm" not in name for name in REGISTRY)


def test_oauth_requests_calendar_event_write_scope():
    assert integrations.CALENDAR_SCOPE.endswith("/calendar.events")
    assert integrations.CALENDAR_SCOPE in integrations.REQUESTED_SCOPES
    assert all(not scope.endswith("/calendar.readonly") for scope in integrations.REQUESTED_SCOPES)
    assert integrations._scope_granted(
        [integrations.CALENDAR_SCOPE], integrations.CALENDAR_READ_SCOPE
    )
    assert not integrations._scope_granted(
        [integrations.CALENDAR_READ_SCOPE], integrations.CALENDAR_SCOPE
    )


def test_calendar_action_reply_links_the_created_event():
    reply = _action_reply(
        {
            "ok": True,
            "kind": "calendar_event",
            "summary": "Nvidia earnings review",
            "start": {"dateTime": "2026-08-11T15:00:00+05:30"},
            "html_link": "https://calendar.google.com/event?eid=one",
        }
    )
    assert "Scheduled Nvidia earnings review" in reply
    assert "[Open in Google Calendar]" in reply
