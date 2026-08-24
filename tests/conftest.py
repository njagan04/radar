import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import BigInteger, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import main as main_module
from db.models import Base


@asynccontextmanager
async def _noop_semaphore(*args, **kwargs):
    """chat.service._investigation_semaphore stand-in — real DistributedSemaphore.acquire()
    needs Lua EVAL, which fakeredis doesn't implement (no lupa backend installed here); the
    concurrency cap itself has nothing test-worthy about it at this layer, so tests just skip
    it entirely rather than pulling in a Lua-capable fake Redis."""
    yield


# SQLite only auto-generates rowid values for a column declared exactly `INTEGER PRIMARY
# KEY` — BigInteger PKs (this schema's convention everywhere) compile to `BIGINT`, which
# does NOT get that autoincrement behavior, so plain inserts without an explicit id would
# fail NOT NULL. Harmless for tests: BIGINT vs INTEGER makes no difference to sqlite's
# storage (both are dynamically-typed integer affinity), only to whether PK autoincrement
# kicks in.
@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


@pytest.fixture
async def chat_db_factory():
    """In-memory sqlite engine, fresh per test — nothing chat-specific needs Postgres-only
    constructs (pg_insert etc.), so this is safe and far faster than real Postgres per test.

    Also attaches a fake "public" schema with minimal stand-ins for WatchTower's own
    public."User"/public."UserProjectAssignment" tables — chat/access.py and
    chat/thread_setup.py run real raw SQL against those (this backend's actual project-access
    and notification-recipient source of truth since 2026-07-29), so tests need something for
    that SQL to hit. Only the columns those two files' queries actually select/join on are
    modeled; use seed_watchtower_access() below to populate them."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS public")
        await conn.exec_driver_sql(
            'CREATE TABLE public."User" (id TEXT PRIMARY KEY, email TEXT NOT NULL)'
        )
        await conn.exec_driver_sql(
            'CREATE TABLE public."UserProjectAssignment" ('
            '"userId" TEXT NOT NULL, "projectName" TEXT NOT NULL, "notifyOnFailure" INTEGER NOT NULL DEFAULT 1)'
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seed_watchtower_access(
    db_factory, user_id: str, email: str, project: str, notify_on_failure: bool = True
) -> None:
    """Populates the fake public."User"/public."UserProjectAssignment" rows chat/access.py's
    require_project_access and chat/thread_setup.py's recipient lookup actually query."""
    async with db_factory() as db:
        await db.execute(
            text('INSERT INTO public."User" (id, email) VALUES (:id, :email)'),
            {"id": user_id, "email": email},
        )
        await db.execute(
            text(
                'INSERT INTO public."UserProjectAssignment" ("userId", "projectName", "notifyOnFailure") '
                "VALUES (:user_id, :project, :notify)"
            ),
            {"user_id": user_id, "project": project, "notify": notify_on_failure},
        )
        await db.commit()


@pytest.fixture
async def chat_app(chat_db_factory):
    """Injects test doubles directly onto app.state, bypassing the real lifespan (which would
    otherwise try to connect to real Postgres/Redis) — main.app is a module-level singleton,
    so this mutates it in place for the duration of the test."""
    main_module.app.state.db_factory = chat_db_factory
    main_module.app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with patch("chat.service._investigation_semaphore", new=_noop_semaphore):
        yield main_module.app


@pytest.fixture
async def chat_client(chat_app):
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
