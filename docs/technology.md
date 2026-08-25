# Tech stack

FastAPI · SQLAlchemy (async) against WatchTower's own Postgres instance (`radar` schema; schema
managed by this repo's own Prisma tooling, not Alembic) · Redis (Upstash in dev) · OpenAI Agents
SDK against Azure AI Foundry's v1 endpoint · `azure-identity` + `azure-mgmt-datafactory`
(per-project client/credential cache — see `mcp_servers/adf/client_cache.py`).
