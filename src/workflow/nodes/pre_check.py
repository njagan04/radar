from workflow.context import WorkflowContext
from workflow.state import InvestigationState

_CANCELLED_STATUSES = {"Cancelled", "Cancelling", "Canceling"}


async def pre_check(state: InvestigationState, ctx: WorkflowContext) -> dict:
    """
    The one remaining hard pre-chat gate (§9 of implementation_plan.md) — everything else
    that used to live here (has_prior_history feeding the classifier) is retired along with
    classifier.py/dependency_check.py. Known-fix reuse and cascade detection are now tools
    the live investigator agent calls itself; loop-prevention is passive context surfaced
    via load_context.py, not a branch computed here.

    TO_IMPLEMENT: distinguish user-cancelled vs system-cancelled vs dependency-cancelled
    using run_status sub-codes and trigger_type. Base case: treat all cancellations uniformly.
    """
    if state.get("run_status") in _CANCELLED_STATUSES:
        return {"error_category": "cancelled"}
    return {}
