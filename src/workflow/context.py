from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker


@dataclass
class WorkflowContext:
    """Plain replacement for LangGraph's `RunnableConfig`/`configurable` dict — there's no
    graph anymore, so nodes just take this directly instead of unpacking a stringly-keyed
    config dict via `get_configurable()`."""

    db_factory: async_sessionmaker
    redis: aioredis.Redis
    approval_actor: str | None = None
