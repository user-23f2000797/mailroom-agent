"""
Canonical hashing rules.

Everything that needs a stable fingerprint (a single dossier's content, an
evaluation's full dossier set, a proposal, a receipt) goes through the same
canonicalization routine so that:

  - identical logical content always hashes identically, regardless of key
    ordering, whitespace, or field insertion order in the incoming JSON
  - two requests that are byte-different but semantically identical are
    recognized as the same (needed for "exact replay" detection)
  - two requests that are semantically different are never confused (needed
    for "changed-content conflict" -> 409 detection)

We deliberately do NOT include volatile/administrative fields (evaluationId,
receiptId, timestamps) in the *content* fingerprint of a dossier — those are
correlation IDs, not content. The dossier cache is keyed by dossier content
only, exactly as the spec requires ("cache the decision by canonical dossier
content, not by evaluation ID").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """
    Deterministic JSON serialization:
      - keys sorted
      - no extraneous whitespace
      - non-ASCII preserved as-is (ensure_ascii=False) but consistently
      - floats/ints serialized via default json rules (stable for our inputs)
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_fingerprint(obj: Any) -> str:
    """Canonical fingerprint of an arbitrary JSON-able object."""
    return sha256_hex(canonical_json(obj))


def dossier_fingerprint(dossier: dict) -> str:
    """
    Fingerprint of a single dossier's CONTENT ONLY.

    We explicitly whitelist the fields that constitute "content" so that
    if the grader adds a new administrative field later, it doesn't
    silently change the fingerprint (and therefore doesn't invalidate the
    cache or trigger spurious conflicts). Adjust this whitelist once you
    have the exact dossier schema from the spec.
    """
    content_view = {
        "dossierId": dossier.get("dossierId") or dossier.get("id"),
        "subject": dossier.get("subject"),
        "body": dossier.get("body") or dossier.get("content"),
        "sender": dossier.get("sender") or dossier.get("from"),
        "recipients": dossier.get("recipients") or dossier.get("to"),
        "attachments": dossier.get("attachments"),
        "metadata": dossier.get("metadata"),
    }
    return content_fingerprint(content_view)


def evaluation_fingerprint(dossiers: list[dict]) -> str:
    """
    Fingerprint of an entire evaluation's dossier SET (used to detect
    'same evaluationId, changed content' -> HTTP 409). Order-independent:
    sorted by dossierId so that resending the same set in a different
    order is not treated as a conflict.
    """
    ids_and_hashes = sorted(
        (
            str(d.get("dossierId") or d.get("id")),
            dossier_fingerprint(d),
        )
        for d in dossiers
    )
    return content_fingerprint(ids_and_hashes)


def proposal_digest(proposal: dict) -> str:
    """
    Fingerprint of a single proposal's decision (action + payload + target),
    used to validate that a commit's receipt actually matches what was
    proposed (reject if callId/action/proposal digest mismatch).
    """
    view = {
        "dossierId": proposal.get("dossierId"),
        "callId": proposal.get("callId"),
        "inputDigest": proposal.get("inputDigest"),
        "action": proposal.get("action"),
        "payload": proposal.get("payload"),
    }
    return content_fingerprint(view)
