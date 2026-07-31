"""
Request/response envelopes.

IMPORTANT — READ THIS:
The source spec references "Exact propose request and response" and "Exact
commit request and terminal response" examples that were not present in the
text I was given (likely stripped images/tables). The field names below
(`dossierId`, `callId`, `receiptKey`, etc.) are a reasonable inference from
the prose spec, NOT copied from a real example. Before you submit, diff this
against the real spec and adjust field names in this file + hashing.py's
`dossier_fingerprint`/`proposal_digest` — those are the only two places
field names are assumed.

Everything here is validated BEFORE any AI/model call and BEFORE any tool
effect, per the "Validate the entire request atomically" / "malformed
schemas must return HTTP 400 or 422 before AI/tool work" requirement.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Action(str, Enum):
    create_draft = "create_draft"
    update_internal_record = "update_internal_record"
    send_approved_notice = "send_approved_notice"
    request_confirmation = "request_confirmation"
    quarantine_item = "quarantine_item"
    no_action = "no_action"


# --------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------

class Dossier(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown fields tolerated, never trusted as instructions

    dossierId: str = Field(min_length=1)
    subject: Optional[str] = None
    body: Optional[str] = None
    content: Optional[str] = None
    sender: Optional[str] = None
    recipients: Optional[list[str]] = None
    attachments: Optional[list[Any]] = None
    metadata: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def must_have_some_body(self):
        if not (self.body or self.content):
            raise ValueError("dossier must include 'body' or 'content'")
        return self


class ProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["propose"]
    evaluationId: str = Field(min_length=1)
    dossiers: list[Dossier] = Field(min_length=1)

    @field_validator("dossiers")
    @classmethod
    def no_duplicate_ids(cls, v: list[Dossier]) -> list[Dossier]:
        ids = [d.dossierId for d in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate dossierId in request")
        return v


class Proposal(BaseModel):
    dossierId: str
    callId: str
    action: Action
    payload: dict[str, Any]
    evidence: list[str]  # smallest set of cited lines proving the decision


class ProposeResponse(BaseModel):
    status: Literal["awaiting_receipts"] = "awaiting_receipts"
    evaluationId: str
    proposals: list[Proposal]


# --------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------

class Receipt(BaseModel):
    model_config = ConfigDict(extra="allow")

    dossierId: str = Field(min_length=1)
    callId: str = Field(min_length=1)
    receiptKey: str = Field(min_length=1)  # unpredictable receipt-verification key
    approved: bool = True


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["commit"]
    evaluationId: str = Field(min_length=1)
    receipts: list[Receipt] = Field(min_length=1)

    @field_validator("receipts")
    @classmethod
    def no_duplicate_receipt_ids(cls, v: list[Receipt]) -> list[Receipt]:
        ids = [r.dossierId for r in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate dossierId in receipts")
        return v


class Outcome(BaseModel):
    dossierId: str
    callId: str
    status: Literal["executed", "rejected", "error"]
    result: dict[str, Any]


class CommitResponse(BaseModel):
    status: Literal["completed"] = "completed"
    evaluationId: str
    outcomes: list[Outcome]


# --------------------------------------------------------------------------
# top-level operation discriminator (read once, dispatch, THEN validate fully)
# --------------------------------------------------------------------------

class OperationPeek(BaseModel):
    model_config = ConfigDict(extra="allow")
    operation: Optional[str] = None
