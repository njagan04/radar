from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker


@dataclass
class WorkflowContext:
    """Per-call dependencies (DB session factory, Redis, the acting approver if any) passed
    directly to each workflow node function — no framework, no config-dict indirection."""

    db_factory: async_sessionmaker
    redis: aioredis.Redis
    approval_actor: str | None = None
