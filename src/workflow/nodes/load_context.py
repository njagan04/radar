from datetime import datetime, timezone

from db.models import AuditLog
from workflow.context import WorkflowContext
from workflow.state import InvestigationState


async def load_context(state: InvestigationState, ctx: WorkflowContext) -> dict:
    """Marks the investigation as started in the audit trail. The project_metadata
    existence check + row fetch already happened in diagnose.py's run_diagnosis()
    before this node runs, so there's nothing left here to re-fetch."""
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
    return {}
