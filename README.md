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
ADF tool calls (66 distinct tools, keyword-retrieval-selected
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

```
radar/
├── src/                            # The application
│   ├── main.py                     # FastAPI app, lifespan (DB/Redis/concurrency-cap setup)
│   │
│   ├── config/
│   │   ├── settings.py             # All configuration, loaded from .env
│   │   └── error_categories.py     # Canonical error categories + the human-action-only subset
│   │
│   ├── db/
│   │   ├── models.py                # SQLAlchemy models — single source of schema truth
│   │   └── rca.py                   # ProjectRCA read/write logic — plain queries, platform-agnostic
│   │
│   ├── gateway/
│   │   ├── rbac.py                 # RBACGateway (the enforcement) + call_tool (routes in-process vs. remote)
│   │   ├── tool_exec_auth.py       # Service-identity JWT for chat-backend <-> server.py
│   │   ├── credential_resolution.py # client_secret resolution from WatchTower's public.Credential
│   │   └── concurrency.py          # DistributedSemaphore — Redis-backed, shared across replicas
│   │
│   ├── intake/
│   │   ├── listener.py             # POST /events/pipeline-failure — WatchTower's entry point
│   │   └── batch_detection.py      # Built, currently unused (batch alerting switched off)
│   │
│   ├── mcp_servers/adf/
│   │   ├── tools/                  # ADF tool implementations (the shared TOOL_REGISTRY)
│   │   ├── schemas/                # 66-distinct-tool declarative specs, one file per resource kind
│   │   ├── tool_search_tool.py     # Keyword-based tool retrieval + build_chat_tools (real FunctionTool wiring)
│   │   └── auth.py                 # Azure credential construction
│   │
│   ├── notifications/
│   │   ├── messages.py             # Notification body text (one template, not per-outcome)
│   │   └── email.py                # Delivery via Microsoft Graph's sendMail
│   │
│   ├── llm/                        # Platform-agnostic agent domain logic — tool-calling, state, execution
│   │   ├── agent.py                # The conversational LLM turn (run/resume, approval handling)
│   │   ├── tools.py                # Generic RCA tools (db/rca.py-backed) + build_tools_for_platform dispatcher
│   │   ├── state.py / state_builder.py  # Shared state shape + reconstructing it from DB rows
│   │   └── context.py              # Per-call dependencies (DB factory, Redis)
│   │
│   ├── chat/                       # Conversational transport — the only diagnosis path
│   │   ├── router.py               # FastAPI endpoints
│   │   ├── service.py              # Business logic behind each endpoint
│   │   ├── thread_setup.py         # Creates the thread + seed message + notification at intake time
│   │   ├── seed_message.py         # The templated (non-LLM) first-message text
│   │   ├── history.py              # Converts stored messages → LLM input format
│   │   ├── summarization.py        # Context-window compaction for long threads
│   │   └── access.py / deps.py     # Auth/authorization checks
│   │
│   └── server.py                   # Separate deployable (own VM/subnet) — POST /tools/{tool_name}/call,
│                                    # runs the SAME RBACGateway; never imported by main.py
│
├── prisma/                          # This repo's own Prisma schema/migrations for the "radar"
│                                    # Postgres schema (moved from watch-Tower 2026-08-04 — see
│                                    # schema.prisma's header comment). `npm install && npx prisma
│                                    # migrate deploy` to apply.
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
├── alembic.ini.retired
└── pyproject.toml
```

## Getting started

This backend's tables live inside WatchTower's own Postgres instance, in the `radar` schema
(`prisma/schema.prisma`, right here in this repo, is the authoritative migration source —
Alembic was used before 2026-07-29, then retired in favor of Prisma; those old migrations were
kept for historical reference for a while and have since been deleted). Point `DATABASE_URL` at
that same Postgres instance.

```bash
poetry install
npm install            # Prisma tooling only — see prisma/schema.prisma
cp .env.example .env   # fill in real values — see config/settings.py for the full list
npx prisma migrate deploy   # applies this repo's own radar-schema migrations
PYTHONPATH=src poetry run python -m uvicorn main:app --reload --app-dir src
```

Or via Docker: `docker compose up --build`.

### Tool-execution service (optional, off by default)

`src/server.py` is a separate deployable — a standalone FastAPI app that runs the same
`RBACGateway` (unchanged) that otherwise runs in-process. Leave
`TOOL_EXEC_SERVICE_URL`/`TOOL_EXEC_ASSERTION_SECRET` unset (the default) for local/
single-process dev — the chat backend then runs `RBACGateway` in-process exactly as before this
existed, no extra service needed.

To run it standalone (its own host/subnet in production; same machine on a different port for
local testing):

```bash
TOOL_EXEC_ASSERTION_SECRET=<same value as the chat backend's> \
  PYTHONPATH=src poetry run python -m uvicorn server:app --port 8100 --app-dir src
```

Then set on the chat backend's own `.env`: `TOOL_EXEC_SERVICE_URL=http://<host>:8100` and the
same `TOOL_EXEC_ASSERTION_SECRET` — `gateway/rbac.py`'s `call_tool()` then routes every tool
call over HTTP to it instead of running the gateway locally. In production, restrict inbound
traffic on that host/port to the chat backend's own subnet only (NSG/security-group rule —
infra-level, not something this code enforces).

## Tech stack

FastAPI · SQLAlchemy (async) against WatchTower's own Postgres instance (`radar` schema; schema managed by this repo's own Prisma tooling, not Alembic) · Redis (Upstash in dev) · OpenAI Agents SDK against Azure AI Foundry's v1 endpoint · `azure-identity` + `azure-mgmt-datafactory` (per-project client/credential cache — see `mcp_servers/adf/client_cache.py`).

## Current status

**Built:** backend foundation (no LangGraph/Teams; per-project ADF credentials resolved from WatchTower's own encrypted Credential table, no Key Vault), the full DB schema for the chat-product design (multi-user chat access, ad-hoc threads, chat memory/context-window management, multi-factory credentials), a distributed Redis-backed concurrency cap, classifier/dependency-check retirement (folded into the investigator agent's own tools), notification delivery code (Microsoft Graph email — wired but not yet actually sending, pending a real Entra ID app registration), and a security-review pass with fixes (a fail-open HMAC bug, a self-referential signature scheme).

**Known, tracked, not yet fixed:** the 5 currently-seeded `rbac_permissions` rows have the wrong `requires_consent` tiering (deferred intentionally until the ADF tool port, when all ~27 tools get seeded at once).

Full detail and the dated decision log: [`xyz/implementation_plan.md`](xyz/implementation_plan.md).

## Next development steps

In dependency order — see `xyz/implementation_plan.md` §16 for the full reasoning:

1. **Authentication (Microsoft SSO / Entra ID)** — foundational for real user identity; blocked on an actual Entra ID app registration (client ID, tenant ID, redirect URI) from the team's own Azure tenant. The same registration also unlocks real outbound email (item above) once `Mail.Send` is granted.
2. **Chat backend core** — the claim endpoint, RLS policies, seed-message rendering, the two entry paths (email deep-link + in-app icon), the tool-consent dialog wired to `apply_rerun`/`apply_denial`, streaming, and the summarization pass for long chats.
3. **Sandboxed tool-dispatch boundary** — contains a compromised-code/prompt-injection scenario by isolating the one step that makes real Azure calls, shared across every thread rather than provisioned per-thread.
4. **ADF tool port** — the remaining ~20 tools from `claude-desktop/`, the checkpoint/rollback system reimplemented on the `resource_snapshot*` tables already in the schema, and correct `rbac_permissions` seeding/tiering for the full set (including fixing the 5 existing rows' `requires_consent` values).
5. **Later, larger, less time-sensitive:** frontend UI, Synapse/Databricks/Fabric platform support, the SOP vector store, approver fallback for an unavailable claimant, and the MCP protocol-conformance fixes found in the `claude-desktop` spec review.
