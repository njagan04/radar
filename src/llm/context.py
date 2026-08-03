from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker


@dataclass
class WorkflowContext:
    """Per-call dependencies (DB session factory, Redis) passed directly to the agent's
    tool-building/execution functions — no framework, no config-dict indirection. `user_id` is
    threaded as an explicit parameter everywhere instead of living on this context
    (run_chat_turn/resume_chat_turn/build_chat_tools)."""

    db_factory: async_sessionmaker
    redis: aioredis.Redis
