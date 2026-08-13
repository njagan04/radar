"""
ProjectRCA read/write logic — plain Postgres queries, no LLM/Agents-SDK concepts involved.
Kept separate from llm/tools.py's @function_tool wrappers (check_known_fix,
record_diagnosis_outcome) so this data-access logic is reusable by anything that needs it later
(a future non-chat entry point, a different agent framework, a script) without dragging in the
Agents SDK. Genuinely platform-agnostic: ProjectRCA is keyed by (pipeline_id, project,
error_signature), none of which are ADF-specific concepts.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ProjectRCA


async def find_known_fix(
    db: AsyncSession,
    pipeline_id: str,
    project: str,
    error_category: str | None = None,
) -> tuple[list[ProjectRCA], ProjectRCA | None]:
    """Returns (this pipeline's own RCA history, a cross-pipeline match sharing error_category
    if one was requested and found)."""
    same_pipeline_result = await db.execute(
        select(ProjectRCA)
        .where(ProjectRCA.pipeline_id == pipeline_id, ProjectRCA.project == project)
        .order_by(ProjectRCA.last_failure_timestamp.desc())
        .limit(5)
    )
    same_pipeline_rows = list(same_pipeline_result.scalars().all())

    cross_pipeline_row: ProjectRCA | None = None
    if error_category:
        cross_result = await db.execute(
            select(ProjectRCA)
            .where(
                ProjectRCA.project == project,
                ProjectRCA.pipeline_id != pipeline_id,
                ProjectRCA.error_category == error_category,
                ProjectRCA.fix_applied.isnot(None),
            )
            .order_by(ProjectRCA.last_failure_timestamp.desc())
            .limit(1)
        )
        cross_pipeline_row = cross_result.scalar_one_or_none()

    return same_pipeline_rows, cross_pipeline_row


async def record_diagnosis_outcome(
    db: AsyncSession,
    pipeline_id: str,
    project: str,
    error_signature: str,
    error_category: str,
    root_cause: str | None = None,
    fix_applied: str | None = None,
) -> tuple[ProjectRCA, bool]:
    """Find-or-create a ProjectRCA row keyed by (pipeline_id, project, error_signature) —
    matches the table's real UniqueConstraint. On a match, updates failure_count/
    last_failure_timestamp/error_category unconditionally, and overwrites root_cause/
    fix_applied only for whichever of those were actually passed (so a later call can add
    detail without clobbering earlier fields with None). Returns (the row, whether it was newly
    created) — does NOT commit; caller owns the transaction."""
    now = datetime.now(tz=UTC)
    existing_result = await db.execute(
        select(ProjectRCA).where(
            ProjectRCA.pipeline_id == pipeline_id,
            ProjectRCA.project == project,
            ProjectRCA.error_signature == error_signature,
        )
    )
    row = existing_result.scalar_one_or_none()

    if row is not None:
        row.error_category = error_category
        row.failure_count += 1
        row.last_failure_timestamp = now
        if root_cause is not None:
            row.root_cause = root_cause
        if fix_applied is not None:
            row.fix_applied = fix_applied
        row.updated_at = now
        return row, False

    row = ProjectRCA(
        pipeline_id=pipeline_id,
        project=project,
        error_signature=error_signature,
        error_category=error_category,
        root_cause=root_cause,
        fix_applied=fix_applied,
        failure_count=1,
        last_failure_timestamp=now,
        updated_at=now,
    )
    db.add(row)
    return row, True
