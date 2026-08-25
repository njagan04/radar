# Dev environment

## Getting started

This backend's tables live inside WatchTower's own Postgres instance, in the `radar` schema
(`prisma/schema.prisma`, right here in this repo, is the authoritative migration source —
Alembic was used before 2026-07-29, then retired in favor of Prisma; those old migrations were
kept for historical reference for a while and have since been deleted). Point `DATABASE_URL` at
that same Postgres instance.

```bash
uv sync --all-groups
npm install            # Prisma tooling only — see prisma/schema.prisma
cp .env.example .env   # fill in real values — see config/settings.py for the full list
npx prisma migrate deploy   # applies this repo's own radar-schema migrations
PYTHONPATH=src uv run python -m uvicorn main:app --reload --app-dir src
```

Or via Docker: `docker compose up --build`.

## Linting & formatting

`ruff` handles both lint and format (`.ruff.toml`). `lefthook.yml` wires it into git hooks —
run `lefthook install` once after cloning to enable pre-commit auto-format and pre-push checks.

```bash
uv run ruff check .            # lint
uv run ruff check --fix .      # lint, auto-fix safe issues
uv run ruff format .           # format
```

## Tool-execution service (optional, off by default)

`src/server.py` is a separate deployable — a standalone FastAPI app that runs the same
`RBACGateway` (unchanged) that otherwise runs in-process. Leave
`TOOL_EXEC_SERVICE_URL`/`TOOL_EXEC_ASSERTION_SECRET` unset (the default) for local/
single-process dev — the chat backend then runs `RBACGateway` in-process exactly as before this
existed, no extra service needed.

To run it standalone (its own host/subnet in production; same machine on a different port for
local testing):

```bash
TOOL_EXEC_ASSERTION_SECRET=<same value as the chat backend's> \
  PYTHONPATH=src uv run python -m uvicorn server:app --port 8100 --app-dir src
```

Then set on the chat backend's own `.env`: `TOOL_EXEC_SERVICE_URL=http://<host>:8100` and the
same `TOOL_EXEC_ASSERTION_SECRET` — `gateway/rbac.py`'s `call_tool()` then routes every tool
call over HTTP to it instead of running the gateway locally. In production, restrict inbound
traffic on that host/port to the chat backend's own subnet only (NSG/security-group rule —
infra-level, not something this code enforces).
