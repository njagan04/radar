"""
Writes the pending seed message onto the FailureEvent row for a newly received pipeline
failure — called directly from intake/listener.py right after that row is inserted
(2026-07-28). Previously this only ran after a human manually triggered the old structured
diagnosis pipeline (workflow/nodes/notifier.py, now retired), which meant nothing notified a
human of a new failure until they somehow already knew to click "Diagnose" on a thread that
didn't exist yet. Now it runs immediately and unconditionally, matching the confirmed product
direction: chat is the only diagnosis path, "Diagnose this failure" is just a suggested first
chat message, not a separate pipeline.

Notification EMAIL DELIVERY MOVED TO WATCHTOWER (2026-07-29) — WatchTower already has a
working send path (Azure Communication Services) and is the one thing that detected the
failure in the first place; this backend sending its own Graph-mail email alongside it would
have produced two emails per failure. This function's job now ends at "who should be
notified", returned to intake/listener.py, which is what actually crosses the HTTP boundary
back to WatchTower. The `notification_ready` audit entry stays regardless — a record of who
this backend determined should be notified, independent of whether WatchTower's send actually
succeeds.

Recipients come from WatchTower's own `public."UserProjectAssignment"` table (2026-07-29) —
a plain cross-schema query, since this table lives in the SAME physical Postgres database as
this backend's own `radar` schema. Deliberately not `UserProjectAccess` (this schema's own
table, still present but now unused) — WatchTower owns "who's assigned to which project" as
the single source of truth, so this backend reads it directly rather than keeping a second,
separately-maintained copy that could drift.

NO ChatThread IS CREATED HERE (2026-08-05) — a failure notification is common and most are
never opened, so eagerly creating a real thread for every one of them left a pile of empty
threads nobody asked for. The seed text lives on FailureEvent.seed_message until a human
actually sends it (or types something else) from the draft chat page opened via the
notification bell, at which point chat/service.py's create_ad_hoc_thread creates the real
ChatThread and stamps FailureEvent.resolved_thread_id — mirroring the same lazy-creation
pattern an ordinary ad-hoc "New Chat" already uses.
"""
import logging
from datetime import datetime, timezone

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
    now = datetime.now(tz=timezone.utc)

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
            # error_signature, not error_code — ProjectRCA.error_code has never been written
            # anywhere (confirmed by grep: read only here, assigned nowhere), so this lookup
            # could never have matched a single row since the table existed. error_signature
            # is the column that's actually populated, and per record_diagnosis_outcome's own
            # consolidation guidance (llm/tools.py), it holds this exact error_code string in
            # the common case — the intake-time auto-logger (intake/listener.py) writes it that
            # way too, so this is the real match target, not a proxy for it.
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
            pipeline_name=pipeline_name, project=project, run_status=run_status,
            last_error=last_error, matching_rca=matching_rca,
        )

        failure_event = await db.get(FailureEvent, investigation_id)
        failure_event.seed_message = seed_text

        db.add(AuditLog(
            investigation_id=investigation_id,
            thread_id=None,  # no thread exists yet — nothing to attribute this to
            pipeline_name=pipeline_name,
            project=project,
            platform=platform,
            timestamp=now,
            event_type="notification_ready",
            user_id=None,  # system-originated (WatchTower intake), no real user to attribute to
            detail={"recipient_user_ids": recipient_user_ids},
        ))
        await db.commit()

    if not recipient_user_ids:
        logger.warning(
            "No WatchTower UserProjectAssignment rows with notifyOnFailure=true for "
            "project=%s — WatchTower will have nobody to email, though chat is still "
            "reachable for anyone with project access. investigation_id=%s",
            project, investigation_id,
        )

    return recipient_user_ids
