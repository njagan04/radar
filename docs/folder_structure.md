# Folder structure

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
