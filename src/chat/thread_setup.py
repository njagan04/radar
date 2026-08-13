"""
Writes the pending seed message onto the FailureEvent row for a newly received pipeline
failure, called from intake/listener.py right after that row is inserted. Chat is the only
diagnosis path — "Diagnose this failure" is just a suggested first chat message, not a
separate pipeline.

Notification email delivery is WatchTower's responsibility (it already has a working send
path and is what detected the failure in the first place). This function's job ends at
determining who should be notified, which intake/listener.py relays back to WatchTower over
HTTP. The `notification_ready` audit entry records that determination regardless of whether
WatchTower's send actually succeeds.

Recipients come from WatchTower's own `public."UserProjectAssignment"` table — a plain
cross-schema query, since that table lives in the same physical Postgres database as this
backend's own `radar` schema. WatchTower owns "who's assigned to which project" as the source
of truth, so this backend reads it directly rather than keeping a separate copy that could
drift.

No ChatThread is created here. The seed text lives on FailureEvent.seed_message until a human
sends it (or types something else) from the draft chat page opened via the notification bell,
at which point chat/service.py's create_ad_hoc_thread creates the real ChatThread and stamps
FailureEvent.resolved_thread_id.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from chat.seed_message import build_seed_message, extract_error_code
from db.models import AuditLog, FailureEvent, ProjectRCA

logger = logging.getLogger(__name__)


async def create_thread_and_notify(
    db_factory: async_sessionmaker,
    investigation_id: str,
    project: str,
    platform: str,
    pipeline_name: str,
    run_status: str,
    last_error: str | None,
    error_detail: dict | None,
) -> list[str]:
    """Returns recipient_user_ids. Writes the suggested first message onto the already-inserted
    FailureEvent row (idempotent: re-running for the same investigation_id just overwrites the
    same row's seed_message rather than creating anything new)."""
    now = datetime.now(tz=UTC)

    async with db_factory() as db:
        result = await db.execute(
            text(
                'SELECT "userId" FROM public."UserProjectAssignment" '
                'WHERE "projectName" = :project AND "notifyOnFailure" = true'
            ),
            {"project": project},
        )
        recipient_user_ids = [str(row[0]) for row in result.all()]

        error_code = extract_error_code(error_detail)
        matching_rca = None
        if error_code:
            # Matches on error_signature, not error_code — ProjectRCA.error_code is never
            # populated. error_signature holds this same error_code string in the common case
            # (see record_diagnosis_outcome in llm/tools.py), and is what intake/listener.py's
            # auto-logger writes too, so it's the real match target.
            rca_result = await db.execute(
                select(ProjectRCA)
                .where(
                    ProjectRCA.pipeline_id == pipeline_name,
                    ProjectRCA.project == project,
                    ProjectRCA.error_signature == error_code,
                )
                .order_by(ProjectRCA.last_failure_timestamp.desc())
                .limit(1)
            )
            matching_rca = rca_result.scalar_one_or_none()

        seed_text = build_seed_message(
            pipeline_name=pipeline_name,
            project=project,
            run_status=run_status,
            last_error=last_error,
            matching_rca=matching_rca,
        )

        failure_event = await db.get(FailureEvent, investigation_id)
        failure_event.seed_message = seed_text

        db.add(
            AuditLog(
                investigation_id=investigation_id,
                thread_id=None,  # no thread exists yet — nothing to attribute this to
                pipeline_name=pipeline_name,
                project=project,
                platform=platform,
                timestamp=now,
                event_type="notification_ready",
                user_id=None,  # system-originated (WatchTower intake), no real user to attribute to
                detail={"recipient_user_ids": recipient_user_ids},
            )
        )
        await db.commit()

    if not recipient_user_ids:
        logger.warning(
            "No WatchTower UserProjectAssignment rows with notifyOnFailure=true for "
            "project=%s — WatchTower will have nobody to email, though chat is still "
            "reachable for anyone with project access. investigation_id=%s",
            project,
            investigation_id,
        )

    return recipient_user_ids
