# Nexus AI

AI-powered pipeline failure investigation and remediation system. When a monitoring app (**WatchTower**) detects an Azure Data Factory pipeline failure, Nexus opens a live chat where a human works with an LLM agent to diagnose the root cause and — where safe and approved — trigger a rerun. Synapse, Databricks, and Fabric are planned; ADF is the only platform built today.

This is not a replacement for WatchTower. WatchTower detects failures; Nexus adds investigation, diagnosis, and remediation on top.

## How it works

```
WatchTower detects failure
        │
        ▼
POST /events/pipeline-failure  (HMAC-signed)
        │
        ▼
Investigation row created, credentials resolved from
per-project Key Vault → cached in Redis
        │
        ▼
Email notification → human opens the chat
        │
        ▼
Live LLM agent investigates using real ADF tool calls
(RBAC-gated, every call logged to the audit trail)
        │
        ▼
Agent proposes a fix → human approves/denies via a
native in-chat consent dialog
        │
        ▼
Approved rerun executes against real ADF infrastructure,
outcome polled and recorded
```

No LangGraph, no Teams — a human is present for the entire diagnose → propose → approve → rerun sequence, so there's no durable pause/resume mechanism or external approval channel to maintain. Orchestration is plain, directly-callable Python (`run_diagnosis` / `apply_rerun` / `apply_denial`), not a graph engine.

## Folder structure

```
nexus/
├── src/                            # The application
│   ├── main.py                     # FastAPI app, lifespan (DB/Redis/concurrency-cap setup)
│   │
│   ├── config/
│   │   ├── settings.py             # All configuration, loaded from .env
│   │   └── error_categories.py     # Canonical error categories + the human-action-only subset
│   │
│   ├── db/
│   │   └── models.py               # SQLAlchemy models — single source of schema truth
│   │
│   ├── gateway/
│   │   ├── rbac.py                 # RBACGateway — per-tool-call permission gate + credential injection
│   │   ├── vault.py                # Key Vault credential resolution → Redis cache
│   │   └── concurrency.py          # DistributedSemaphore — Redis-backed, shared across replicas
│   │
│   ├── intake/
│   │   ├── listener.py             # POST /events/pipeline-failure — WatchTower's entry point
│   │   └── batch_detection.py      # Built, currently unused (batch alerting switched off)
│   │
│   ├── mcp_servers/adf/
│   │   ├── tools.py                # ADF tool implementations (the shared TOOL_REGISTRY)
│   │   ├── auth.py                 # Azure credential construction
│   │   └── server.py               # Stdio MCP server — NOT RBAC-gated, a known tracked gap
│   │
│   ├── notifications/
│   │   ├── messages.py             # Notification body text (one template, not per-outcome)
│   │   └── email.py                # Delivery via Microsoft Graph's sendMail
│   │
│   └── workflow/
│       ├── state.py                # InvestigationState shape
│       ├── context.py              # WorkflowContext — per-call dependencies (DB, Redis, actor)
│       ├── diagnose.py             # run_diagnosis / apply_rerun / apply_denial — the orchestration
│       └── nodes/
│           ├── pre_check.py            # Cancellation short-circuit (the one hard pre-chat gate)
│           ├── load_context.py         # Audit trail + prior-RCA context for loop-prevention
│           ├── investigator.py         # The LLM agent — evidence gathering + structured RCA output
│           ├── notifier.py             # Builds and fires the "needs review" notification
│           ├── rerun.py                # Executes an approved rerun, polls its outcome
│           └── handle_denial.py        # Records a denied fix
│
├── migrations/                     # Alembic migrations (auto-generated + hand-fixed where needed)
├── tests/                          # pytest suite
├── claude-desktop/                 # Standalone R&D tool exploring MCP + checkpoint/rollback —
│                                    # separate venv, not part of src/, source for the future ADF tool port
├── xyz/                             # Design docs & decision log (read implementation_plan.md first)
│   ├── implementation_plan.md         # THE architecture reference + dated decision log
│   ├── data/data_model.md              # Full schema reference, table by table
│   ├── data/erDiagram.mmd              # ER diagram (Mermaid — paste into mermaid.live or view in VS Code)
│   ├── need_to_implement.txt           # Tracked backlog, one topic at a time
│   ├── qa.txt                          # Scratch discussion log
│   └── security_scenarios.md           # Credential-isolation scenario walkthroughs
│
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
└── pyproject.toml
```

## Getting started

```bash
poetry install
cp .env.example .env   # fill in real values — see config/settings.py for the full list
poetry run alembic upgrade head
PYTHONPATH=src poetry run uvicorn main:app --reload --app-dir src
```

Or via Docker: `docker compose up --build`.

## Tech stack

FastAPI · SQLAlchemy (async) + Postgres (Neon in dev) · Redis (Upstash in dev) · OpenAI Agents SDK against Azure AI Foundry's v1 endpoint · Azure Key Vault + `azure-identity` · Alembic.

## Current status

**Built:** backend foundation (no LangGraph/Teams, per-project Key Vault credentials), the full DB schema for the chat-product design (multi-user chat access, ad-hoc threads, chat memory/context-window management, multi-factory credentials), a distributed Redis-backed concurrency cap, classifier/dependency-check retirement (folded into the investigator agent's own tools), notification delivery code (Microsoft Graph email — wired but not yet actually sending, pending a real Entra ID app registration), and a security-review pass with fixes (a fail-open HMAC bug, a self-referential signature scheme).

**Known, tracked, not yet fixed:** `mcp_servers/adf/server.py`'s stdio path bypasses RBAC entirely (needs a decision: gate it too, or restrict it to local-only); the 5 currently-seeded `rbac_permissions` rows have the wrong `requires_consent` tiering (deferred intentionally until the ADF tool port, when all ~27 tools get seeded at once).

Full detail and the dated decision log: [`xyz/implementation_plan.md`](xyz/implementation_plan.md).

## Next development steps

In dependency order — see `xyz/implementation_plan.md` §16 for the full reasoning:

1. **Authentication (Microsoft SSO / Entra ID)** — foundational for real user identity; blocked on an actual Entra ID app registration (client ID, tenant ID, redirect URI) from the team's own Azure tenant. The same registration also unlocks real outbound email (item above) once `Mail.Send` is granted.
2. **Chat backend core** — the claim endpoint, RLS policies, seed-message rendering, the two entry paths (email deep-link + in-app icon), the tool-consent dialog wired to `apply_rerun`/`apply_denial`, streaming, and the summarization pass for long chats.
3. **Sandboxed tool-dispatch boundary** — contains a compromised-code/prompt-injection scenario by isolating the one step that makes real Azure calls, shared across every thread rather than provisioned per-thread.
4. **ADF tool port** — the remaining ~20 tools from `claude-desktop/`, the checkpoint/rollback system reimplemented on the `resource_snapshot*` tables already in the schema, and correct `rbac_permissions` seeding/tiering for the full set (including fixing the 5 existing rows' `requires_consent` values).
5. **Later, larger, less time-sensitive:** frontend UI, Synapse/Databricks/Fabric platform support, the SOP vector store, approver fallback for an unavailable claimant, and the MCP protocol-conformance fixes found in the `claude-desktop` spec review.
