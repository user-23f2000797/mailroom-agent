"""
The actual tool effects. These are stubs — plug in your real draft-store /
internal-record-store / outbound-send integrations here. The important
contract each function must honor:

  - Idempotent by call_id: db.effect_already_executed(call_id) is checked
    by the caller (logic.py) BEFORE invoking these, so a replayed commit
    never re-executes a real effect. These functions assume that check has
    already passed.
  - Never receive raw dossier content — only the model's structured,
    safety-gated payload. This is what keeps "never put raw mail, secret
    canaries, or unrelated text into tool arguments" true by construction:
    the payload is all these functions ever see.
  - request_confirmation / quarantine_item / no_action have no external
    side effect — "executing" them just means recording that the queue/
    isolation/suppression decision was made.
"""

from __future__ import annotations

from typing import Any


async def execute_create_draft(payload: dict[str, Any]) -> dict:
    # TODO: wire to your real draft queue / CRM. Stub: record intent only.
    return {
        "effect": "draft_created",
        "queue": payload.get("queue"),
        "recipient": payload.get("recipient"),
        "subject": payload.get("subject"),
    }


async def execute_update_internal_record(payload: dict[str, Any]) -> dict:
    # TODO: wire to your real internal record store, one authorized field only.
    return {
        "effect": "record_updated",
        "recordId": payload.get("recordId"),
        "field": payload.get("field"),
        "newValue": payload.get("newValue"),
    }


async def execute_send_approved_notice(payload: dict[str, Any]) -> dict:
    # TODO: wire to your real outbound send integration. This path should
    # only ever be reached for dossiers that passed the trusted-approval
    # gate in safety.py — treat that gate as load-bearing, not optional.
    return {
        "effect": "notice_sent",
        "recipient": payload.get("recipient"),
        "template": payload.get("template"),
    }


async def execute_request_confirmation(payload: dict[str, Any]) -> dict:
    return {"effect": "routed_to_queue", "queue": payload.get("queue")}


async def execute_quarantine_item(payload: dict[str, Any]) -> dict:
    return {"effect": "quarantined", "reason": payload.get("reason")}


async def execute_no_action(payload: dict[str, Any]) -> dict:
    return {"effect": "no_action", "reason": payload.get("reason")}


DISPATCH = {
    "create_draft": execute_create_draft,
    "update_internal_record": execute_update_internal_record,
    "send_approved_notice": execute_send_approved_notice,
    "request_confirmation": execute_request_confirmation,
    "quarantine_item": execute_quarantine_item,
    "no_action": execute_no_action,
}


async def execute(action: str, payload: dict[str, Any]) -> dict:
    fn = DISPATCH.get(action)
    if fn is None:
        return {"effect": "error", "reason": f"unknown action {action}"}
    return await fn(payload)
