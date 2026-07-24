from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuditLog, ProjectRCA
from workflow.context import WorkflowContext
from workflow.state import InvestigationState


async def load_context(state: InvestigationState, ctx: WorkflowContext) -> dict:
    """
    Marks the investigation as started in the audit trail, and fetches a lightweight
    summary of this pipeline's most recent RCA row — not to make a routing decision (that
    logic retired along with classifier.py), just to surface as passive loop-prevention
    context in the investigator's system prompt, per §10 of implementation_plan.md: "the
    decision stays with the agent/human in conversation rather than being gated in Python
    before anyone sees it." The investigator's own check_known_fix tool can do a fuller,
    fresher lookup during its reasoning if it wants more than this summary.
    """
    async with ctx.db_factory() as db:
        db.add(AuditLog(
            investigation_id=state["investigation_id"],
            pipeline_id=state["pipeline_name"],
            project=state["project"],
            platform=state["platform"],
            timestamp=datetime.now(tz=timezone.utc),
            event_type="investigation_started",
            actor="system",
            detail={
                "run_status": state["run_status"],
                "failure_count": state["failure_count"],
                "trigger_type": state.get("trigger_type"),
            },
        ))
        await db.commit()

        result = await db.execute(
            select(ProjectRCA)
            .where(
                ProjectRCA.pipeline_id == state["pipeline_name"],
                ProjectRCA.project == state["project"],
            )
            .order_by(ProjectRCA.last_failure_timestamp.desc())
            .limit(1)
        )
        top: ProjectRCA | None = result.scalar_one_or_none()

    if top is None:
        return {"prior_rca_context": None}

    return {
        "prior_rca_context": {
            "error_category": top.error_category,
            "fix_applied": top.fix_applied,
            "invocation_count": top.invocation_count,
            "denial_count": len(top.denial_history or []),
            "last_rerun_attempt": top.last_rerun_attempt,
        }
    }
