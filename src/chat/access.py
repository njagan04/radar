"""
App-level access control for chat endpoints. Postgres RLS is an explicitly deferred
follow-up hardening pass, not built this milestone — every endpoint must call one of these
two functions itself rather than assuming the database enforces anything.

Reads WatchTower's own public."UserProjectAssignment" (joined through public."User" by
email) — same cross-schema source as thread_setup.py's recipient lookup, so "who can see this
project" and "who gets emailed about it" never drift apart (2026-07-29, switched from this
schema's own now-unused UserProjectAccess table).
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ChatThread


async def require_project_access(db: AsyncSession, user_email: str, project: str) -> None:
    result = await db.execute(
        text(
            'SELECT 1 FROM public."User" u '
            'JOIN public."UserProjectAssignment" upa ON upa."userId" = u.id '
            'WHERE u.email = :email AND upa."projectName" = :project'
        ),
        {"email": user_email, "project": project},
    )
    if result.first() is None:
        raise HTTPException(status_code=403, detail=f"No access to project '{project}'")


async def require_thread_access(db: AsyncSession, user_email: str, thread: ChatThread) -> None:
    """Read access — any project member. Write access (claim-gated) is checked separately,
    inline in service.py's post_message/approve/deny, since it depends on claim state that
    read access alone doesn't capture."""
    await require_project_access(db, user_email, thread.project)


async def require_admin(db: AsyncSession, user_email: str) -> None:
    """Cross-project access, deliberately independent of UserProjectAssignment — an admin sees
    every project's data, not just ones they're individually assigned to monitor."""
    result = await db.execute(
        text('SELECT 1 FROM public."User" WHERE email = :email AND "isAdmin" = true'),
        {"email": user_email},
    )
    if result.first() is None:
        raise HTTPException(status_code=403, detail="Admin access required")
