# Mailroom Agent

A FastAPI service implementing the propose → grader-receipt → commit workflow
described in the spec: one public endpoint, two operations, an AI classifier
per dossier, and a code-level safety/persistence layer around it.

## ⚠️ Read this first — assumptions you MUST verify

The spec text referenced "Exact propose request and response" / "Exact
commit request and terminal response" examples that weren't present in what
I was given (likely stripped images/tables). Everything below was built
against a **reasonable inference**, not the real schema. Before you submit:

1. **Field names** — `dossierId`, `callId`, `receiptKey`, `evaluationId`,
   `payload`, `evidence` are my best guesses at the wire format. Check the
   real spec and adjust in **`app/schemas.py`** (the pydantic models) — that's
   the only file that defines wire field names.
2. **Receipt verification** — the spec says "Store each evaluation's
   supplied receipt-verification key with that evaluation" but doesn't show
   where that key comes from. I implemented a self-consistent design (see
   the big comment at the top of **`app/receipt_tokens.py`**): the agent
   itself mints an HMAC token per proposal and expects the grader's receipt
   to echo it back. **If the real spec has the grader supplying its own
   opaque key instead, this is the file to change** — the storage plumbing
   in `db.py` already supports either approach, only `verify_receipt_token`
   needs to change.
3. **Dossier content fields** — I assumed `subject` / `body` (or `content`)
   / `sender` / `recipients` / `attachments` / `metadata`. Adjust
   `dossier_fingerprint()` in **`app/hashing.py`** and the `Dossier` model in
   `app/schemas.py` to match the real fields, especially anything the audit
   dossiers use to signal "trusted approval" (see point 4).
4. **`send_approved_notice` trust marker** — `app/safety.py`'s
   `has_trusted_approval_marker()` currently checks for
   `dossier.metadata.trustedApproval` / `approvalStatus`. This is the
   single most important safety gate in the whole system (an unauthorized
   outbound proposal caps your score at 0.75/4), so get this field name
   right once you see a real "approved to send" dossier example.

Everything else (caching by content hash, replay handling, conflict
detection, receipt/commit idempotency, timeouts, response-size cap, input
validation order) is spec-literal and shouldn't need changes.

## Architecture

```
app/
  main.py            FastAPI entrypoint — single POST /agent endpoint
  schemas.py          Pydantic request/response envelopes (validation gate)
  logic.py             propose()/commit() orchestration
  ai.py                 The only place the model is called (OpenAI API)
  safety.py             Code-level guardrails applied AFTER the model
  hashing.py            Canonical content fingerprints (cache keys, digests)
  db.py                 SQLite persistence (durable across restarts)
  receipt_tokens.py     HMAC receipt issuance/verification
  tools.py              Stub tool executors (wire to real systems here)
```

Request flow for `propose`:
1. Parse JSON → validate operation → validate full schema (400/422 on failure)
2. Check if this `evaluationId` was seen before:
   - same content → return the exact stored response (no model call)
   - different content → 409
3. For each dossier: check the **content-hash cache** first. On miss, call
   the model, then run it through `safety.py`'s guardrails, then cache.
4. Persist the evaluation + proposals; return `awaiting_receipts`.

Request flow for `commit`:
1. Parse JSON → validate (400/422 on failure)
2. If this exact commit was already processed, return the stored terminal
   response (no re-execution).
3. Otherwise, look up the evaluation's proposals; for each receipt, check
   structural match (callId/digest) + HMAC token; reject invalid ones
   without executing anything.
4. Execute the (safety-gated) action via `tools.py`, idempotent by `callId`.
5. Persist the terminal response; return `completed`.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in OPENAI_API_KEY
```

Test your OpenAI connection before anything else:

```bash
export OPENAI_API_KEY=your_key_here
python3 test_openai_connection.py
```

Run locally:

```bash
export OPENAI_API_KEY=your_key_here
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health check: `curl http://localhost:8000/health`

## Deploying

### Render (recommended — supports real persistent disks)

1. **Push this project to a GitHub repo** (Render deploys from Git):
   ```bash
   git init
   git add .
   git commit -m "mailroom agent"
   # create a repo on github.com, then:
   git remote add origin https://github.com/<you>/mailroom-agent.git
   git branch -M main
   git push -u origin main
   ```

2. **Sign up / log in at [render.com](https://render.com)** and connect your GitHub account.

3. **New + → Blueprint**, point it at your repo — Render will read `render.yaml`
   automatically and pre-fill everything (Docker runtime, health check path,
   persistent disk at `/app/data`).

   *(Alternatively: New + → Web Service → select the repo → Render
   auto-detects the `Dockerfile` → set env vars manually per the table below.)*

4. **Set your secret**: in the service's Environment tab, add
   `OPENAI_API_KEY` = your key (this is the one variable `render.yaml`
   deliberately leaves blank — never commit API keys to Git).

5. **Plan matters for persistence**: `render.yaml` defaults to the `starter`
   plan (~$7/mo) because persistent disks require a paid instance. If you
   switch `plan: free` in `render.yaml` (or pick Free in the UI), it still
   deploys and works — but `/app/data` resets whenever the instance spins
   down from ~15 min of inactivity, meaning your cache/replay state won't
   survive gaps between Checks. For a graded run where cost is trivial
   anyway, starter is the safer choice.

6. **Deploy.** Render gives you a URL like `https://mailroom-agent.onrender.com`.
   Confirm it's alive:
   ```bash
   curl https://mailroom-agent.onrender.com/health
   ```
   Your submission URL is `https://mailroom-agent.onrender.com/agent`.

### Other hosts

Fly.io and Railway both also support persistent volumes and read the same
`Dockerfile` directly if Render isn't your preference — the container
itself doesn't care which platform runs it.

## Concurrency note

This uses SQLite with a single `asyncio.Lock` around writes and a single
uvicorn worker. That's intentional — it makes durability and idempotency
easy to reason about, and the grader's load (64+6 dossiers, two evaluations)
doesn't need more throughput than that. If you need to scale this beyond the
exam, swap `db.py`'s backend for Postgres (the function signatures are
already a clean seam for that) and drop the in-process lock in favor of
proper transactions.

## What's still a stub

`app/tools.py`'s four "real effect" functions (`create_draft`,
`update_internal_record`, `send_approved_notice`) just record structured
intent rather than calling real systems — there's no draft queue / internal
record store / outbound mailer described in the spec to integrate with.
They're wired in exactly the shape you'd plug real integrations into, and
the idempotency-by-`callId` guarantee (checked in `logic.py` before these
are ever called) holds regardless of what you put inside them.
