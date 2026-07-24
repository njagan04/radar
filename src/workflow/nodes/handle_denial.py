import logging
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuditLog, ProjectRCA
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


async def handle_denial(state: InvestigationState, ctx: WorkflowContext) -> dict:
    db_factory = ctx.db_factory
    approval_actor: str = ctx.approval_actor or "unknown"
    now = datetime.now(tz=timezone.utc)

    rca_id = state.get("rca_id")
    denial_count = 0

    if rca_id is not None:
        try:
            async with db_factory() as db:
                rca_result = await db.execute(select(ProjectRCA).where(ProjectRCA.id == rca_id))
                rca: ProjectRCA | None = rca_result.scalar_one_or_none()
                if rca is not None:
                    denial_entry = {
                        "timestamp": now.isoformat(),
                        "actor": approval_actor,
                        "fix_applied": rca.fix_applied,
                        "error_category": state.get("error_category"),
                    }
                    history = list(rca.denial_history or [])
                    history.append(denial_entry)
                    rca.denial_history = history
                    denial_count = len(history)

                    db.add(AuditLog(
                        investigation_id=state["investigation_id"],
                        pipeline_id=state["pipeline_name"],
                        project=state["project"],
                        platform=state["platform"],
                        timestamp=now,
                        event_type="rerun_denied",
                        actor=approval_actor,
                        detail={
                            "rca_id": rca_id,
                            "denial_count": denial_count,
                            "fix_applied": rca.fix_applied,
                        },
                    ))
                    await db.commit()
        except Exception:
            logger.exception(
                "Failed to record denial: investigation_id=%s rca_id=%s",
                state["investigation_id"],
                rca_id,
            )
    else:
        # No RCA on this investigation (shouldn't normally happen once investigator has run,
        # but handle it rather than assume) — still audit the denial
        async with db_factory() as db:
            db.add(AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=now,
                event_type="rerun_denied",
                actor=approval_actor,
                detail={"rca_id": None, "error_category": state.get("error_category")},
            ))
            await db.commit()

    return {"thread_status": "completed"}
