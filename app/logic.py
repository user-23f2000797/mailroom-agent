"""
Core propose/commit orchestration. This is where all the pieces
(hashing, db, ai, safety, receipt_tokens, tools) meet.

Ordering within propose() and commit() is deliberate and mirrors the
"build order" in the spec: validate -> check replay/conflict -> cache
lookup -> AI (only on cache miss) -> safety gates -> persist -> respond.
Nothing durable is written before validation passes; no tool effect is
ever executed before a receipt is verified.
"""

from __future__ import annotations

from typing import Any

from . import ai, db, safety
from .hashing import (
    dossier_fingerprint,
    evaluation_fingerprint,
    proposal_digest,
)
from .receipt_tokens import verify_receipt_token
from .schemas import CommitRequest, Outcome, Proposal, ProposeRequest
from .tools import execute


class ConflictError(Exception):
    """Same evaluationId, different content -> HTTP 409."""


class UnknownEvaluationError(Exception):
    """Commit references an evaluationId we never proposed -> HTTP 409."""


class ReceiptMismatchError(Exception):
    """A receipt's callId/action/digest doesn't match the persisted proposal."""


def _call_id_for(content_hash: str) -> str:
    # Deterministic from content only -> stable across evaluations/Checks,
    # as required ("Stable dossiers must produce the same complete proposal
    # and callId across evaluations and later Checks").
    return f"call_{content_hash[:24]}"


async def _decide_one(dossier: dict) -> dict:
    """
    Returns {"dossier_id", "call_id", "action", "payload", "evidence"} for a
    single dossier, using the cache when possible and never calling the
    model twice for the same canonical content.
    """
    dossier_id = str(dossier.get("dossierId"))
    content_hash = dossier_fingerprint(dossier)

    cached = await db.get_cached_decision(content_hash)
    if cached is not None:
        return cached

    raw_decision = await ai.classify_dossier(dossier)
    gated_decision = safety.apply_safety_gates(dossier, raw_decision)

    call_id = _call_id_for(content_hash)
    result = {
        "dossier_id": dossier_id,
        "call_id": call_id,
        "action": gated_decision["action"],
        "payload": gated_decision["payload"],
        "evidence": gated_decision["evidence"],
    }

    await db.put_cached_decision(
        content_hash=content_hash,
        dossier_id=dossier_id,
        call_id=call_id,
        action=result["action"],
        payload=result["payload"],
        evidence=result["evidence"],
    )
    return result


async def propose(request: ProposeRequest) -> dict:
    eval_id = request.evaluationId
    dossiers_raw = [d.model_dump() for d in request.dossiers]
    fingerprint = evaluation_fingerprint(dossiers_raw)

    existing = await db.get_evaluation(eval_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise ConflictError(
                f"evaluationId {eval_id} previously seen with different dossier content"
            )
        # exact replay: return the byte-equivalent stored response, no model work
        return existing["response"]

    proposals: list[dict] = []
    for dossier in dossiers_raw:
        decision = await _decide_one(dossier)
        proposal = {
            "dossierId": decision["dossier_id"],
            "callId": decision["call_id"],
            "action": decision["action"],
            "payload": decision["payload"],
            "evidence": decision["evidence"],
        }
        # validate against the Proposal schema before it ever leaves this process
        Proposal.model_validate(proposal)
        proposals.append(proposal)

        digest = proposal_digest(proposal)
        # Pre-register the receipt row (unverified) so commit-time lookups
        # always have a persisted proposal to check against, even before
        # any receipt has arrived.
        await db.put_receipt(
            evaluation_id=eval_id,
            dossier_id=decision["dossier_id"],
            call_id=decision["call_id"],
            receipt_key_hash="",  # filled in at commit time once a receipt arrives
            proposal_digest=digest,
            verified=False,
        )

    response = {
        "status": "awaiting_receipts",
        "evaluationId": eval_id,
        "proposals": proposals,
    }
    await db.put_evaluation(eval_id, fingerprint, response)
    return response


async def commit(request: CommitRequest) -> dict:
    eval_id = request.evaluationId

    existing_commit = await db.get_commit_response(eval_id)
    if existing_commit is not None:
        return existing_commit  # exact replay, no re-execution

    evaluation = await db.get_evaluation(eval_id)
    if evaluation is None:
        raise UnknownEvaluationError(f"no propose phase found for evaluationId {eval_id}")

    proposals_by_dossier = {p["dossierId"]: p for p in evaluation["response"]["proposals"]}

    outcomes: list[dict] = []
    for receipt in request.receipts:
        dossier_id = receipt.dossierId
        proposal = proposals_by_dossier.get(dossier_id)

        if proposal is None:
            outcomes.append(
                {
                    "dossierId": dossier_id,
                    "callId": receipt.callId,
                    "status": "rejected",
                    "result": {"reason": "no matching proposal for this dossierId in this evaluation"},
                }
            )
            continue

        digest = proposal_digest(proposal)
        stored = await db.get_receipt(eval_id, dossier_id)

        structural_ok = (
            receipt.callId == proposal["callId"]
            and (stored is None or stored["proposal_digest"] == digest)
        )
        supplied_key = getattr(receipt, "receiptKey", None)
        crypto_ok = verify_receipt_token(eval_id, dossier_id, proposal["callId"], digest, supplied_key)

        if not (structural_ok and crypto_ok and receipt.approved):
            outcomes.append(
                {
                    "dossierId": dossier_id,
                    "callId": receipt.callId,
                    "status": "rejected",
                    "result": {"reason": "invalid or unapproved receipt"},
                }
            )
            continue

        # Persist the (now-verified) receipt before executing any effect.
        await db.put_receipt(
            evaluation_id=eval_id,
            dossier_id=dossier_id,
            call_id=proposal["callId"],
            receipt_key_hash=supplied_key or "",
            proposal_digest=digest,
            verified=True,
        )

        if await db.effect_already_executed(proposal["callId"]):
            existing = stored["outcome"] if stored and stored["outcome"] else {"effect": "already_executed"}
            outcomes.append(
                {
                    "dossierId": dossier_id,
                    "callId": proposal["callId"],
                    "status": "executed",
                    "result": existing,
                }
            )
            continue

        result = await execute(proposal["action"], proposal["payload"])
        await db.record_effect_executed(proposal["callId"], proposal["action"])
        await db.mark_committed(eval_id, dossier_id, result)

        outcomes.append(
            {
                "dossierId": dossier_id,
                "callId": proposal["callId"],
                "status": "executed",
                "result": result,
            }
        )

    response = {
        "status": "completed",
        "evaluationId": eval_id,
        "outcomes": outcomes,
    }
    await db.put_commit_response(eval_id, response)
    return response
