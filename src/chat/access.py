"""
App-level access control for chat endpoints. Postgres does not enforce access control here,
so every endpoint must call one of these two functions itself.

Keyed on the verified X-Radar-Assertion JWT's `id` claim (WatchTower's own public.User.id),
not email — id is the stable, authoritative identity for authorization decisions.

Reads WatchTower's own public."UserProjectAssignment" directly — same cross-schema source as
thread_setup.py's recipient lookup.
"""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ChatThread


async def require_project_access(db: AsyncSession, user_id: str, project: str) -> None:
    # No join needed: UserProjectAssignment.userId already IS the id being checked.
    result = await db.execute(
        text(
            'SELECT 1 FROM public."UserProjectAssignment" '
            'WHERE "userId" = :user_id AND "projectName" = :project'
        ),
        {"user_id": user_id, "project": project},
    )
    if result.first() is None:
        raise HTTPException(status_code=403, detail=f"No access to project '{project}'")


async def require_thread_access(
    db: AsyncSession, user_id: str, thread: ChatThread
) -> None:
    """Read access — any project member. Write access (claim-gated) is checked separately,
    inline in service.py's post_message/approve/deny, since it depends on claim state that
    read access alone doesn't capture."""
    await require_project_access(db, user_id, thread.project)


async def require_admin(db: AsyncSession, user_id: str) -> None:
    """Cross-project access, deliberately independent of UserProjectAssignment — an admin sees
    every project's data, not just ones they're individually assigned to monitor."""
    result = await db.execute(
        text('SELECT 1 FROM public."User" WHERE id = :user_id AND "isAdmin" = true'),
        {"user_id": user_id},
    )
    if result.first() is None:
        raise HTTPException(status_code=403, detail="Admin access required")
