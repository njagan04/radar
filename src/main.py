from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.router import router as chat_router
from config.settings import settings
from gateway.middleware import RateLimitingMiddleware, SecurityHeadersMiddleware
from intake.listener import router as events_router
from llm.embeddings import embed_texts_async


@asynccontextmanager
async def lifespan(app: FastAPI):
    # server_settings sets search_path for every connection in the pool — asyncpg doesn't
    # accept a raw libpq "options=" query param via SQLAlchemy's URL (SQLAlchemy forwards
    # unrecognized query params straight to asyncpg.connect(), which has no such kwarg; only
    # a full DSN string parsed by asyncpg itself understands "options=", which this isn't).
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=settings.db_pool_pre_ping,
        connect_args={"server_settings": {"search_path": settings.radar_db_schema}},
    )
    app.state.db_factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.redis = await aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        retry_on_timeout=True,
        health_check_interval=30,
    )

    # Eagerly loads the local MiniLM embedding model (tool retrieval, injection detection)
    # here instead of lazily on whatever request happens to need it first, so a real user's
    # first turn doesn't pay the load cost.
    await embed_texts_async(["warmup"])

    yield

    await app.state.redis.aclose()
    await engine.dispose()


_is_production = settings.environment == "production"
app = FastAPI(
    title="RADAR",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)
# Order matters — Starlette's add_middleware() prepends to its internal list, so the LAST
# middleware added ends up OUTERMOST (runs first on the way in, last on the way out) — the
# reverse of call order. SecurityHeadersMiddleware is added last so it wraps everything,
# meaning a 429 from RateLimitingMiddleware (added first, so it's inner) still passes back
# through SecurityHeadersMiddleware and gets its headers on the way out.
app.add_middleware(
    RateLimitingMiddleware, calls_per_minute=settings.rate_limit_per_minute
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(events_router)
app.include_router(chat_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
