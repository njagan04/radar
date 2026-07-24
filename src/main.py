from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from gateway.concurrency import DistributedSemaphore
from intake.listener import router as events_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine(settings.database_url, echo=False)
    app.state.db_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = await aioredis.from_url(settings.redis_url, decode_responses=True)
    # Distributed, not per-process — shared across every replica via Redis, so the cap holds
    # regardless of how many copies are running (see gateway/concurrency.py).
    app.state.semaphore = DistributedSemaphore(
        redis=app.state.redis,
        key="active_investigations",
        max_count=settings.max_concurrent_investigations,
        lease_seconds=settings.concurrency_lease_seconds,
        max_wait_seconds=settings.concurrency_max_wait_seconds,
    )

    yield

    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="Nexus AI", version="0.1.0", lifespan=lifespan)
app.include_router(events_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
