"""
Distributed concurrency cap for live diagnosis sessions, shared across every replica.

Replaces the old asyncio.BoundedSemaphore(max_concurrent_investigations), which only capped
concurrency per-process — correct for one replica, silently multiplied by replica count once
this runs on Kubernetes with more than one copy. What this cap actually protects (Azure OpenAI's
rate quota, the Postgres connection pool, cost) is shared across every replica regardless of how
many are running, so the cap itself has to be shared too.

Implementation: a Redis sorted set, one member per currently-running diagnosis, scored by
acquire time. Acquire prunes anything older than `lease_seconds` (a crashed replica's slot
self-heals this way — no heartbeat/renewal needed, since this is a soft resource-protection
cap, not a hard mutual-exclusion lock), then adds the new member only if still under the cap.
Prune+count+add is one atomic Lua script — two replicas can't both slip past the cap in the
same instant.

Cap-hit behavior: queue with backoff, not a hard reject. The scenario that actually fills the
cap — a real incident correlating many failures, many humans clicking Diagnose in the same few
minutes — is exactly the moment a hard rejection hurts most. Retries with exponential backoff
up to `max_wait_seconds`; only raises ConcurrencyLimitExceeded if it's still full after that.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable
from typing import cast

import redis.asyncio as aioredis

_ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local lease_seconds = tonumber(ARGV[2])
local max_count = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - lease_seconds)
local count = redis.call('ZCARD', KEYS[1])
if count < max_count then
    redis.call('ZADD', KEYS[1], now, member)
    return 1
else
    return 0
end
"""


class ConcurrencyLimitExceeded(RuntimeError):
    """Raised when no slot became available within max_wait_seconds."""


class DistributedSemaphore:
    def __init__(
        self,
        redis: aioredis.Redis,
        key: str,
        max_count: int,
        lease_seconds: float,
        max_wait_seconds: float,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 10.0,
    ):
        self._redis = redis
        self._key = key
        self._max_count = max_count
        self._lease_seconds = lease_seconds
        self._max_wait_seconds = max_wait_seconds
        self._initial_backoff_seconds = initial_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._member: str | None = None

    async def _try_acquire(self, member: str) -> bool:
        # redis-py types eval() as Union[Awaitable[str], str] — a stub imprecision shared
        # between its sync and async clients; the async client always actually returns an
        # awaitable (returning a Lua integer, which redis-py decodes as a real int at
        # runtime regardless of the str-typed stub — verified live). Cast, don't rewrite.
        result = await cast(
            Awaitable[int],
            self._redis.eval(
                _ACQUIRE_SCRIPT,
                1,
                self._key,
                str(time.time()),
                str(self._lease_seconds),
                str(self._max_count),
                member,
            ),
        )
        return bool(result)

    async def acquire(self) -> None:
        member = str(uuid.uuid4())
        deadline = time.monotonic() + self._max_wait_seconds
        backoff = self._initial_backoff_seconds
        while True:
            if await self._try_acquire(member):
                self._member = member
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConcurrencyLimitExceeded(
                    f"No diagnosis slot available within {self._max_wait_seconds}s "
                    f"(cap={self._max_count})"
                )
            await asyncio.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, self._max_backoff_seconds)

    async def release(self) -> None:
        if self._member is not None:
            await self._redis.zrem(self._key, self._member)
            self._member = None

    async def __aenter__(self) -> "DistributedSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()
