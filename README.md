# RADAR

AI-powered pipeline failure investigation and remediation system. When a monitoring app (**WatchTower**) detects an Azure Data Factory pipeline failure, RADAR opens a live chat where a human works with an LLM agent to diagnose the root cause and — where safe and approved — trigger a rerun. Synapse, Databricks, and Fabric are planned; ADF is the only platform built today.

This is not a replacement for WatchTower. WatchTower detects failures; RADAR adds investigation, diagnosis, and remediation on top.

## How it works

```
WatchTower detects failure
        │
        ▼
POST /events/pipeline-failure  (HMAC-signed)
        │
        ▼
FailureEvent row created; client_secret resolved per tool
call straight from WatchTower's own encrypted Credential row
        │
        ▼
ChatThread + seed message created immediately, email sent
→ human opens the chat (no manual "diagnose" trigger needed)
        │
        ▼
Human asks a question, or sends a suggested prompt like
"diagnose this failure" — handled like any other message
        │
        ▼
The conversational LLM agent investigates/acts using real
ADF tool calls (44 distinct tools, keyword-retrieval-selected
per turn, RBAC-gated, every call logged to the audit trail)
        │
        ▼
A mutating tool call (e.g. rerun_pipeline) pauses the run and
shows a native in-chat approval prompt — human approves/denies
        │
        ▼
Approved calls execute against real ADF infrastructure through
the same RBAC-gated gateway as every other tool call — no
per-tool special-casing (e.g. no dedicated rerun idempotency/
freshness/outcome-polling logic; a human approves every mutating
call individually, which is the actual safety mechanism)
```

No LangGraph, no Teams, and no separate structured pre-chat diagnosis pipeline — the conversational chat agent is the only diagnosis path. A human is present for the entire investigate → act → approve sequence, driven entirely through chat, so there's no durable pause/resume mechanism or external approval channel to maintain.

## Folder structure

See [`docs/folder_structure.md`](docs/folder_structure.md) for the full tree.

## Getting started

```bash
uv sync --all-groups
npm install            # Prisma tooling only — see prisma/schema.prisma
cp .env.example .env   # fill in real values — see config/settings.py for the full list
npx prisma migrate deploy   # applies this repo's own radar-schema migrations
PYTHONPATH=src uv run python -m uvicorn main:app --reload --app-dir src
```

Or via Docker: `docker compose up --build`.

Full setup details (Postgres schema, linting/formatting, the optional standalone
tool-execution service): [`docs/dev-env.md`](docs/dev-env.md).

## Tech stack

FastAPI · SQLAlchemy (async) · Redis · OpenAI Agents SDK · Azure SDKs — full detail in
[`docs/technology.md`](docs/technology.md).

## Current status

**Built and working:** event ingestion with HMAC signature verification, credential resolution straight from WatchTower's own encrypted `Credential` table (no Key Vault), the full chat/RBAC/audit data model, a distributed Redis-backed concurrency cap, the full 44-tool ADF set with correctly seeded `rbac_permissions` (`allowed`/`requires_consent` per tool), and the chat consent/approval flow via the OpenAI Agents SDK's native tool-approval mechanism.

**Not yet built:** real Microsoft SSO/OIDC authentication (identity currently comes from a short-lived JWT WatchTower's own backend mints, not an independent SSO integration RADAR performs itself), a sandboxed tool-dispatch boundary isolating real Azure calls from the rest of the request, the SOP vector store, and the frontend UI.

Full architecture detail: [`xyz/implementation_plan.md`](xyz/implementation_plan.md) — see its "Known gaps" section for the complete current list.

## Next development steps

1. **Authentication** — real Microsoft SSO/OIDC, replacing the current WatchTower-minted-assertion as the sole identity signal.
2. **Sandboxed tool-dispatch boundary** — isolate the step that makes real Azure calls from the trusted control plane (RBAC checks, credential resolution, audit writes), so a prompt-injection or compromised-dependency scenario can't reach live DB/Redis handles.
3. **SOP vector store** — ingest/embed project SOP docs so the agent can ground answers in them.
4. **Later, larger, less time-sensitive:** frontend UI, Synapse/Databricks/Fabric platform support, and an approver-fallback mechanism for an unavailable thread claimant.
