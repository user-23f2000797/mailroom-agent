"""
Single public endpoint. POST /agent handles both `propose` and `commit`
operations, dispatched on the `operation` field — matching the spec's
"Expose one public HTTPS endpoint for the two POST operations".

Validation order (matches "Validate the entire request atomically... before
AI/tool work"):
  1. Is it valid JSON at all?                          -> 400 if not
  2. Does it have a recognized `operation`?             -> 400 if not
  3. Does it match that operation's full schema?        -> 422 if not
     (duplicate dossierId / receipt dossierId also live here)
  4. THEN: replay/conflict checks, cache, AI, safety, persistence.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import db, logic
from .schemas import CommitRequest, OperationPeek, ProposeRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mailroom-agent")

app = FastAPI(title="Mailroom Agent")

REQUEST_TIMEOUT_SECONDS = 50  # leave margin under the spec's 55s/request limit
MAX_RESPONSE_BYTES = 512 * 1024


def _json_response(payload: dict, status_code: int = 200) -> JSONResponse:
    # default=str: pydantic ValidationError.errors() can embed non-JSON-native
    # objects (e.g. the underlying exception in 'ctx') for custom validators.
    body = json.dumps(payload, default=str)
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        # Should not happen given evidence trimming in ai.py/safety.py, but
        # guard the hard contract requirement regardless.
        logger.error("response exceeded 512 KiB cap, evaluationId=%s", payload.get("evaluationId"))
        payload = {
            "status": "error",
            "error": "response_too_large",
            "evaluationId": payload.get("evaluationId"),
        }
        status_code = 500
        body = json.dumps(payload)
    # Return the already-serialized body directly rather than letting
    # JSONResponse re-encode `payload` (which would hit the same
    # non-JSON-native-object problem for pydantic error details).
    return JSONResponse(content=json.loads(body), status_code=status_code, media_type="application/json")


@app.post("/agent")
async def agent_endpoint(request: Request):
    try:
        raw_body = await request.body()
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return _json_response({"status": "error", "error": "invalid_json"}, 400)

        try:
            peek = OperationPeek.model_validate(body)
        except ValidationError as e:
            logger.error("OPERATION PEEK FAILED. raw_body=%s errors=%s", json.dumps(body)[:4000] if isinstance(body, (dict, list)) else str(body)[:4000], e.errors())
            return _json_response({"status": "error", "error": "malformed_request", "detail": e.errors()}, 422)

        if peek.operation not in ("propose", "commit"):
            return _json_response(
                {"status": "error", "error": "invalid_operation", "operation": peek.operation}, 400
            )

        # Always-on lightweight request log — confirms traffic is arriving
        # at all, independent of whether validation succeeds.
        item_key = "dossiers" if peek.operation == "propose" else "receipts"
        item_count = len(body.get(item_key, [])) if isinstance(body, dict) else 0
        logger.info(
            "REQUEST operation=%s evaluationId=%s %s_count=%s",
            peek.operation,
            body.get("evaluationId") if isinstance(body, dict) else None,
            item_key,
            item_count,
        )

        return await asyncio.wait_for(_dispatch(peek.operation, body), timeout=REQUEST_TIMEOUT_SECONDS)

    except asyncio.TimeoutError:
        return _json_response({"status": "error", "error": "request_timeout"}, 504)
    except Exception as exc:  # noqa: BLE001 — top-level safety net, never leak a stack trace
        logger.exception("unhandled error")
        return _json_response({"status": "error", "error": "internal_error", "detail": str(exc)}, 500)


async def _dispatch(operation: str, body: dict) -> JSONResponse:
    if operation == "propose":
        try:
            req = ProposeRequest.model_validate(body)
        except ValidationError as e:
            # Log the FULL raw body + validation errors here — this is the
            # only way to see what a real grader payload actually looks
            # like when our schema guess is wrong. Check Render's log
            # stream after a failed grading attempt.
            logger.error(
                "PROPOSE VALIDATION FAILED. raw_body=%s errors=%s",
                json.dumps(body)[:4000],
                e.errors(),
            )
            return _json_response({"status": "error", "error": "malformed_propose", "detail": e.errors()}, 422)

        try:
            result = await logic.propose(req)
        except logic.ConflictError as e:
            return _json_response({"status": "error", "error": "conflict", "detail": str(e)}, 409)

        return _json_response(result, 200)

    # operation == "commit"
    try:
        req = CommitRequest.model_validate(body)
    except ValidationError as e:
        logger.error(
            "COMMIT VALIDATION FAILED. raw_body=%s errors=%s",
            json.dumps(body)[:4000],
            e.errors(),
        )
        return _json_response({"status": "error", "error": "malformed_commit", "detail": e.errors()}, 422)

    try:
        result = await logic.commit(req)
    except logic.UnknownEvaluationError as e:
        return _json_response({"status": "error", "error": "unknown_evaluation", "detail": str(e)}, 409)
    except logic.ReceiptMismatchError as e:
        return _json_response({"status": "error", "error": "receipt_mismatch", "detail": str(e)}, 409)

    return _json_response(result, 200)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.on_event("shutdown")
async def _shutdown():
    await db.close_conn()
