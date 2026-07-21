from sqlalchemy import select

from db.models import ProjectRCA
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

_CANCELLED_STATUSES = {"Cancelled", "Cancelling", "Canceling"}


async def pre_check(state: InvestigationState, ctx: WorkflowContext) -> dict:
    # Cancellation short-circuit: skip all investigation, route straight to notifier.
    # TO_IMPLEMENT: distinguish user-cancelled vs system-cancelled vs dependency-cancelled
    # using run_status sub-codes and trigger_type. Base case: treat all cancellations uniformly.
    if state.get("run_status") in _CANCELLED_STATUSES:
        return {
            "has_prior_history": None,
            "classification_bucket": 0,
            "classification_reasoning": "pipeline_cancelled",
            "error_category": "cancelled",
            "requires_human_action": False,
            "known_fix": None,
            "cross_pipeline_match": None,
            "cross_pipeline_source": None,
        }

    async with ctx.db_factory() as db:
        result = await db.execute(
            select(ProjectRCA.id)
            .where(
                ProjectRCA.pipeline_id == state["pipeline_name"],
                ProjectRCA.project == state["project"],
            )
            .limit(1)
        )
        row = result.scalar_one_or_none()
    return {"has_prior_history": row is not None}