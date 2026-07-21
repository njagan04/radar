import logging
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuditLog, ProjectContact
from notifications import messages
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


async def notifier(state: InvestigationState, ctx: WorkflowContext) -> dict:
    """
    Decides which message applies (cancelled / human-action / loop-prevention / cascade /
    cross-pipeline-known-fix / known-fix / rca-complete) and whether a rerun approval is
    needed, then writes it to the audit log. No delivery here — the Outlook deep-link email
    and the chat seed message (next milestone) read `notification_ready` audit entries to
    render the actual notification/seed message.
    """
    db_factory = ctx.db_factory
    bucket = state.get("classification_bucket")
    upstream_also_failed = state.get("upstream_also_failed")

    async with db_factory() as db:
        result = await db.execute(
            select(ProjectContact).where(
                ProjectContact.project == state["project"],
                ProjectContact.contact_type == "primary_approver",
            )
        )
        contact: ProjectContact | None = result.scalar_one_or_none()

    assigned_user_email = contact.assigned_user_email if contact else None

    needs_approval = False

    if state.get("error_category") == "cancelled":
        notify_type = "pipeline_cancelled"
        body = messages.cancelled_body(state["pipeline_name"], state["project"])

    elif state.get("requires_human_action"):
        notify_type = "human_action_required"
        body = messages.human_action_required_body(
            state["pipeline_name"], state["project"], state.get("error_category"), state.get("last_error")
        )

    elif bucket == 0:
        notify_type = "loop_prevention"
        body = messages.loop_prevention_body(state["pipeline_name"], state["project"], state.get("error_category"))

    elif upstream_also_failed:
        notify_type = "cascade_confirmed"
        body = messages.cascade_confirmed_body(state["pipeline_name"], state["project"])

    elif bucket == 1 and state.get("cross_pipeline_match"):
        notify_type = "cross_pipeline_known_fix"
        body = messages.cross_pipeline_known_fix_body(
            state["pipeline_name"], state["project"], state.get("error_category"),
            state.get("cross_pipeline_source"), state.get("known_fix"),
        )
        needs_approval = True

    elif bucket == 1:
        notify_type = "known_fix"
        body = messages.known_fix_body(
            state["pipeline_name"], state["project"], state.get("error_category"), state.get("known_fix")
        )
        needs_approval = True

    else:
        notify_type = "rca_complete"
        body = messages.rca_complete_body(state["pipeline_name"], state["project"], state.get("investigation_summary"))
        needs_approval = True

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
                "notify_type": notify_type,
                "body": body,
                "needs_approval": needs_approval,
                "assigned_user_email": assigned_user_email,
            },
        ))
        await db.commit()

    return {"notify_sent": True, "needs_approval": needs_approval, "proposed_at": proposed_at}
