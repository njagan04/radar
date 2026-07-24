import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from config.settings import settings
from db.models import AuditLog, ProjectContact
from notifications import messages
from notifications.email import send_investigation_email
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


async def notifier(state: InvestigationState, ctx: WorkflowContext) -> dict:
    """
    One notification body regardless of outcome (2026-07-24, simplified further) — the email's
    only job is getting the human into the chat; the specifics (cancelled / human-action-needed
    / investigation summary) render once they open it, via the seed message. `needs_approval`/
    `proposed_at` stay as internal fields — they drive the chat's own rerun-consent flow, which
    is a functional decision, not a notification-copy decision.

    Delivery: fire-and-forget via asyncio.create_task so this doesn't hold up run_diagnosis (or
    the concurrency semaphore slot it runs under) waiting on an email round-trip. The audit
    trail (`notification_ready`) is written regardless of whether the send actually succeeds —
    delivery is a convenience layer on top of it, not the source of truth.
    """
    db_factory = ctx.db_factory

    async with db_factory() as db:
        result = await db.execute(
            select(ProjectContact).where(
                ProjectContact.project == state["project"],
                ProjectContact.contact_type == "primary_approver",
            )
        )
        contact: ProjectContact | None = result.scalar_one_or_none()

    assigned_user_email = contact.assigned_user_email if contact else None

    # Not cancelled and not permanently-human-only -> the investigator's fix is a rerun
    # candidate, so the chat should offer the approve/deny consent flow.
    needs_approval = not (
        state.get("error_category") == "cancelled" or state.get("requires_human_action")
    )

    chat_url = f"{settings.nexus_base_url}/chat/{state['investigation_id']}"
    body = messages.investigation_notification_body(state["pipeline_name"], state["project"], chat_url)

    now = datetime.now(tz=timezone.utc)
    proposed_at = now.isoformat() if needs_approval else None

    async with db_factory() as db:
        db.add(AuditLog(
            investigation_id=state["investigation_id"],
            pipeline_id=state["pipeline_name"],
            project=state["project"],
            platform=state["platform"],
            timestamp=now,
            event_type="notification_ready",
            actor="notifier",
            detail={
                "body": body,
                "needs_approval": needs_approval,
                "assigned_user_email": assigned_user_email,
            },
        ))
        await db.commit()

    if assigned_user_email:
        subject = f"Pipeline failure: {state['pipeline_name']} ({state['project']})"
        asyncio.create_task(send_investigation_email(assigned_user_email, subject, body))
    else:
        logger.warning(
            "No primary_approver contact for project=%s — skipping email, chat is still reachable "
            "for anyone with project access. investigation_id=%s",
            state["project"], state["investigation_id"],
        )

    return {"notify_sent": True, "needs_approval": needs_approval, "proposed_at": proposed_at}
