# closing-orchestration-service

Production-shaped orchestration API for a digital mortgage closing platform (lender, title, borrower, notary). This repository is designed for **workflow analysis**, **cross-service reasoning**, and **security posture review** in AI-assisted engineering environments.

## Stack

| Layer | Choice |
|--------|--------|
| API | FastAPI (async) |
| Persistence | PostgreSQL + SQLAlchemy 2.x (`asyncpg` API; `psycopg2` in workers) |
| Background work | Celery + Redis |
| HTTP callbacks | `httpx` (sync in workers) |

## Workflow

Ordered states:

`draft` → `docs_ready` → `borrower_review` → `signing_scheduled` → `signed` → `funding_ready` → `closed`

Transitions are enforced in `app/services/workflow_engine.py` (single-step forward, plus `funding_ready` → `closed`). Partner webhooks may propose `target_state` with intentionally loose coupling.

### Document integrity invariant

A `documents_packaged` event reports that files are available; it does not prove that the expected files arrived. Before advancing a closing, orchestration must SHA-256 the staged PDF/TIFF bytes and require every expected digest to match, so a substituted or corrupted package cannot continue through the workflow. Hash file contents even when a document is empty, never derive the digest from its name, path, or borrower metadata, and keep tests isolated to synthetic data and test-owned temporary directories rather than cleaning a shared staging root. Staging must be shared with the worker and immutable after packaging; a matching digest detects byte drift against the recorded Result but cannot authenticate this demo's unsigned webhook publisher.

## Run locally

1. Start infrastructure:

   ```bash
   docker compose up -d postgres redis
   ```

2. Configure environment — copy `.env.example` to `.env` and adjust if needed.

3. Install dependencies (Python 3.11+ recommended):

   ```bash
   pip install -r requirements.txt
   ```

4. Run API:

   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

5. Run Celery worker (separate terminal):

   ```bash
   celery -A app.tasks.celery_app worker -l info
   ```

6. Run Celery beat (separate terminal) so committed-but-unpublished verification jobs are reconciled:

   ```bash
   celery -A app.tasks.celery_app beat -l info
   ```

Open docs at `http://localhost:8000/docs`.

### Quick integration smoke

Create a closing, assign a notary (queues LOS callback job if `los_callback_url` set), post a partner webhook, read status:

```bash
curl -s -X POST localhost:8000/closings/create -H "Content-Type: application/json" \
  -d "{\"lender_org_id\":\"lender-demo\",\"external_ref\":\"LOS-1001\",\"los_callback_url\":\"https://httpbin.org/get\"}"

curl -s localhost:8000/closings/<CLOSING_UUID>/status

curl -s -X POST localhost:8000/closings/<CLOSING_UUID>/assign-notary -H "Content-Type: application/json" \
  -d "{\"notary_id\":\"notary-42\",\"trigger_los_sync\":true}"

curl -s -X POST localhost:8000/webhooks/partner -H "Content-Type: application/json" \
  -d "{\"partner_id\":\"los-acme\",\"event_type\":\"documents_packaged\",\"external_ref\":\"LOS-1001\",\"payload\":{\"documents\":[{\"filename\":\"package.pdf\",\"expected_sha256\":\"<64-hex-digest-of-staged-bytes>\"}]}}"
```

## HTTP surface

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/closings/create` | Create `ClosingCase` |
| GET | `/closings/{id}/status` | Aggregate status, assignments, funding snapshot, recent workflow events |
| POST | `/closings/{id}/assign-notary` | Create `NotaryAssignment`; advances workflow toward signing |
| POST | `/closings/{id}/reminders` | Enqueue borrower-facing reminder task |
| POST | `/webhooks/partner` | Ingest unsigned LOS / CRM callbacks (`PartnerWebhookEvent`) |
| GET | `/admin/queue-status` | Celery inspect payload (**broken RBAC** — see below) |
| GET | `/debug/config` | Exposes configuration (**unsafe**) |

## Data model (abbrev.)

- **`ClosingCase`** — core loan/closing aggregate with optional `los_callback_url`.
- **`WorkflowEvent`** — append-only audit of transitions.
- **`NotaryAssignment`** — signing agent routing.
- **`PartnerWebhookEvent`** — inbound partner payloads (raw JSON + header snapshot).
- **`ClosingDocument`** — per-package expected/actual SHA-256 Result and verification status.
- **`FundingChecklist`** — gating material toward `funding_ready` / `closed`.

## Background jobs

| Task | Module | Role |
|------|--------|------|
| `cos.send_reminder` | `app/tasks/jobs.py` | Stub reminder fan-out |
| `cos.sync_los_callback` | same | Outbound GET to stored callback (**SSRF demo**) |
| `cos.evaluate_funding` | same | Sync evaluation toward funding readiness |
| `cos.verify_document_package` | same | Verify one persisted manifest, then release `draft` only if every Result matches |
| `cos.reconcile_document_hash_dispatches` | same | Republish durable pending/failed verification dispatches |

## Security posture (intentional weaknesses)

This codebase embeds **deliberate vulnerabilities** for training and tool evaluation. **Do not expose to the internet or real borrower data.**

| Topic | Location / behavior |
|-------|----------------------|
| Unsigned webhooks | `/webhooks/partner` accepts arbitrary JSON with no HMAC or signing secret verification. |
| Replayable webhooks | `idempotency_key` is stored but duplicates are not rejected. |
| Hardcoded API material | `HARDCODED_FALLBACK_API_KEY` and `resolve_internal_api_key()` in `app/config.py`. |
| Broken RBAC (admin) | `require_admin` in `app/deps.py` allows trivial bypasses (`role=admin` query, weak tokens). |
| SSRF | `los_callback_url` on closings is fetched from Celery (`fetch_callback_preview`). |
| Debug exposure | `/debug/config` returns secrets / resolved key tails. |

Hardening checklist for real deployments: enforce webhook signatures with timestamps & replay caches; strict egress controls and URL allowlists for callbacks; OAuth2/OIDC + RBAC from an identity provider; remove debug routes; secrets only from a vault / managed stores.

## Layout

```
app/
  main.py              # FastAPI app factory
  config.py            # Settings + deliberate insecure helpers
  database.py          # Async engine/session
  worker_db.py         # Sync session for Celery
  deps.py              # Auth helpers (flawed admin gate)
  models/              # SQLAlchemy models
  schemas/             # Pydantic request/response models
  routers/             # closings, webhooks, admin, debug
  services/            # Workflow, notary, funding, partner ingest, callbacks
  tasks/               # celery_app + jobs
```

## License

Demo / educational use. Replace insecure patterns before any production use.
