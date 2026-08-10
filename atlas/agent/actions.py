"""Durable two-step Google writes. The model can propose; only this module executes."""

import hashlib
import json
import re
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from atlas.db import integrations
from atlas.db.client import db
from atlas.db.models import PendingAction, User, utcnow
from atlas.llm import provider
from atlas.services import google

CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "string", "enum": ["confirm", "amend", "other"]}},
    "required": ["decision"],
}
CELL = re.compile(r"^([A-Za-z]+)(\d+)(?::[A-Za-z]+\d+)?$")
BATCH_ROWS = 50


async def propose(
    user_id: Any, spec: dict[str, Any], kind: str = "sheet_write"
) -> PendingAction:
    if kind not in {"sheet_write", "calendar_event"}:
        raise ValueError(f"unsupported action kind: {kind}")
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    key = "sha256:" + hashlib.sha256(f"{user_id}:{kind}:{canonical}".encode()).hexdigest()
    summary = proposal_summary(spec, kind)

    await db().pending_actions.update_many(
        {"user_id": user_id, "status": "pending", "idempotency_key": {"$ne": key}},
        {"$set": {"status": "expired"}, "$unset": {"proposed_at": ""}},
    )
    doc = {
        "user_id": user_id,
        "kind": kind,
        "idempotency_key": key,
        "spec": spec,
        "summary": summary,
        "status": "pending",
        "rows_written": 0,
        "error": None,
        "proposed_at": utcnow(),
        "confirmed_at": None,
        "completed_at": None,
    }
    try:
        result = await db().pending_actions.insert_one(doc)
        return PendingAction(**{**doc, "_id": result.inserted_id})
    except DuplicateKeyError:
        existing = await db().pending_actions.find_one({"idempotency_key": key})
        if existing and existing.get("status") in {"expired", "failed"}:
            existing = await db().pending_actions.find_one_and_update(
                {"_id": existing["_id"]},
                {"$set": {"status": "pending", "proposed_at": utcnow(), "error": None},
                 "$unset": {"confirmed_at": "", "completed_at": ""}},
                return_document=ReturnDocument.AFTER,
            )
        return PendingAction(**existing)


def proposal_summary(spec: dict[str, Any], kind: str = "sheet_write") -> str:
    if kind == "calendar_event":
        attendees = [str(value) for value in spec.get("attendees") or []]
        reminders = [int(value) for value in spec.get("reminder_minutes") or []]
        extras = []
        if attendees:
            extras.append("invite " + ", ".join(attendees))
        if reminders:
            extras.append(
                "remind " + ", ".join(f"{minutes} minutes before" for minutes in reminders)
            )
        suffix = f"; {'; '.join(extras)}" if extras else ""
        return (
            f"Schedule '{spec.get('summary', 'Untitled event')}' from {spec.get('start')} "
            f"to {spec.get('end')}{suffix}. Nothing will be created until you confirm."
        )
    count = len(spec.get("values") or [])
    target = str(spec.get("target") or "Sheet1")
    mode = spec.get("mode")
    if mode == "new_tab":
        return f"Add {count} rows to a new tab '{target}'. Nothing existing will be overwritten."
    if mode == "append":
        return f"Append {count} rows to '{target}'. Existing rows will not be changed."
    return f"Overwrite {count} rows starting at {target}."


async def pending_for_user(user_id: Any) -> PendingAction | None:
    doc = await db().pending_actions.find_one(
        {"user_id": user_id, "status": {"$in": ["pending", "executing"]}},
        sort=[("proposed_at", -1), ("confirmed_at", -1)],
    )
    if not doc:
        return None
    if doc.get("status") == "executing":
        doc = await db().pending_actions.find_one_and_update(
            {"_id": doc["_id"], "status": "executing"},
            {"$set": {"status": "pending", "proposed_at": utcnow(),
                      "error": "The previous execution was interrupted; confirmation is required again."},
             "$unset": {"confirmed_at": ""}},
            return_document=ReturnDocument.AFTER,
        )
    action = PendingAction(**doc)
    if not action.proposed_at or action.proposed_at < utcnow() - timedelta(minutes=10):
        await db().pending_actions.update_one(
            {"_id": action.id, "status": "pending"},
            {"$set": {"status": "expired"}, "$unset": {"proposed_at": ""}},
        )
        return None
    return action


async def classify_reply(user_text: str, summary: str) -> str:
    if not user_text.strip():
        return "other"
    raw = await provider.generate(
        system=(
            "Classify a reply to a proposed external action. Confirm only when the user "
            "unmistakably agrees to the exact proposal. If they change any detail, choose amend. "
            "Questions, new topics, hesitation, and ambiguous replies are other. Return JSON."
        ),
        prompt=f"Proposal: {summary}\nUser reply: {user_text}",
        temperature=0,
        json_schema=CONFIRM_SCHEMA,
    )
    try:
        decision = json.loads(raw).get("decision", "other")
        return decision if decision in {"confirm", "amend", "other"} else "other"
    except (json.JSONDecodeError, AttributeError):
        return "other"


async def confirm_and_execute(user: User, action: PendingAction) -> dict[str, Any]:
    claimed = await db().pending_actions.find_one_and_update(
        {"_id": action.id, "user_id": user.id, "status": "pending"},
        {"$set": {"status": "executing", "confirmed_at": utcnow(), "error": None},
         "$unset": {"proposed_at": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        return {
            "ok": False,
            "kind": action.kind,
            "error": "That action is no longer pending.",
            "rows_written": 0,
        }
    current = PendingAction(**claimed)
    required_scope = (
        integrations.CALENDAR_SCOPE
        if current.kind == "calendar_event"
        else integrations.SPREADSHEETS_SCOPE
    )
    token = await integrations.google_access_token(user.id, required_scope)
    if not token:
        capability = "Calendar" if current.kind == "calendar_event" else "Sheets"
        return await _retryable(
            current, f"Google is not connected or the {capability} permission expired."
        )

    try:
        result = (
            await _execute_calendar_event(current, token)
            if current.kind == "calendar_event"
            else await _execute_rows(current, token)
        )
    except Exception as exc:
        return await _retryable(current, f"The action was interrupted ({type(exc).__name__}).")
    if "error" in result:
        return await _retryable(current, str(result["error"]), int(result.get("rows_written") or 0))

    written = int(result.get("rows_written") or 0)
    await db().pending_actions.update_one(
        {"_id": current.id, "status": "executing"},
        {"$set": {"status": "done", "rows_written": written, "completed_at": utcnow(), "error": None}},
    )
    if current.kind == "calendar_event":
        return {
            "ok": True,
            "kind": current.kind,
            "rows_written": 0,
            "summary": result.get("summary", current.spec.get("summary", "Calendar event")),
            "start": result.get("start", current.spec.get("start", "")),
            "html_link": result.get("html_link", ""),
        }
    return {
        "ok": True,
        "kind": current.kind,
        "rows_written": written,
        "target": current.spec.get("target", "Sheet1"),
    }


async def _retryable(action: PendingAction, error: str, rows_written: int | None = None) -> dict[str, Any]:
    written = action.rows_written if rows_written is None else rows_written
    await db().pending_actions.update_one(
        {"_id": action.id},
        {"$set": {"status": "pending", "proposed_at": utcnow(), "rows_written": written, "error": error},
         "$unset": {"confirmed_at": ""}},
    )
    return {
        "ok": False,
        "kind": action.kind,
        "error": error,
        "rows_written": written,
        "retryable": True,
    }


async def _execute_rows(action: PendingAction, token: str) -> dict[str, Any]:
    spec = dict(action.spec)
    spreadsheet_id = str(spec["spreadsheet_id"])
    mode = str(spec["mode"])
    target = str(spec.get("target") or "Sheet1")
    values = [list(row) for row in spec.get("values") or []]
    written = int(action.rows_written or 0)

    if mode == "new_tab":
        titles = await google.spreadsheet_titles(token, spreadsheet_id)
        if isinstance(titles, dict) and "error" in titles:
            return {"error": titles["error"], "rows_written": written}
        created_by_action = bool(spec.get("new_tab_created"))
        if target in titles and not created_by_action:
            return {"error": f"A tab named '{target}' already exists; nothing was overwritten.", "rows_written": written}
        if target not in titles:
            spec["new_tab_created"] = True
            await db().pending_actions.update_one({"_id": action.id}, {"$set": {"spec": spec}})
            created = await google.add_sheet(token, spreadsheet_id, target)
            if "error" in created:
                return {"error": created["error"], "rows_written": written}
        start_row = 1
        base = f"'{_escape_tab(target)}'!A"
    elif mode == "append":
        tab = target.split("!", 1)[0] or "Sheet1"
        start_row = spec.get("resolved_start_row")
        if not start_row:
            existing = await google.sheet_values(token, spreadsheet_id, f"'{_escape_tab(tab)}'!A:ZZ")
            if isinstance(existing, dict) and "error" in existing:
                return {"error": existing["error"], "rows_written": written}
            start_row = len(existing) + 1
            spec["resolved_start_row"] = start_row
            await db().pending_actions.update_one({"_id": action.id}, {"$set": {"spec": spec}})
        base = f"'{_escape_tab(tab)}'!A"
    else:
        base, start_row = target_start(target)
        if start_row is None:
            return {"error": "update_cells target must be an A1 range", "rows_written": written}

    while written < len(values):
        batch = values[written : written + BATCH_ROWS]
        range_ = f"{base}{int(start_row) + written}"
        response = await google.update_values(token, spreadsheet_id, range_, batch)
        if "error" in response:
            return {"error": response["error"], "rows_written": written}
        written += len(batch)
        await db().pending_actions.update_one({"_id": action.id}, {"$set": {"rows_written": written}})
    return {"rows_written": written}


async def _execute_calendar_event(action: PendingAction, token: str) -> dict[str, Any]:
    spec = dict(action.spec)
    event_id = action.idempotency_key.removeprefix("sha256:")[:48]
    return await google.create_calendar_event(
        token,
        summary=str(spec["summary"]),
        start=str(spec["start"]),
        end=str(spec["end"]),
        description=str(spec.get("description") or ""),
        attendees=[str(value) for value in spec.get("attendees") or []],
        location=str(spec.get("location") or ""),
        timezone=str(spec.get("timezone") or ""),
        reminder_minutes=[int(value) for value in spec.get("reminder_minutes") or []],
        event_id=event_id,
    )


def target_start(target: str) -> tuple[str, int | None]:
    if "!" in target:
        tab, cell = target.rsplit("!", 1)
        prefix = f"{tab}!"
    else:
        cell, prefix = target, ""
    match = CELL.match(cell)
    return (f"{prefix}{match.group(1)}", int(match.group(2))) if match else ("", None)


def _escape_tab(tab: str) -> str:
    return tab.strip("'").replace("'", "''")
