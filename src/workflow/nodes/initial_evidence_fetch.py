import logging

from gateway.rbac import RBACGateway, infra_params
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)

# Per-platform initial diagnostic tool.
# Runs before classification so the classifier and investigator see the real
# leaf-level failure, not just "Execute Pipeline activity failed."
#
# For ADF: resolves the master run, then recursively follows any ExecutePipeline
# chains (Master → Pipeline B → Pipeline C → Copy Activity) and returns the
# full execution path + leaf failure detail.
#
# The result overwrites error_detail in state. If the ADF call fails, the
# WatchTower-provided error_detail is left untouched (non-fatal).
_PLATFORM_TOOL: dict[str, str] = {
    "adf": "get_activity_run_error",
}


async def initial_evidence_fetch(state: InvestigationState, ctx: WorkflowContext) -> dict:
    platform = state["platform"]
    tool_name = _PLATFORM_TOOL.get(platform)
    if not tool_name:
        # Platform not yet supported — leave error_detail as received from WatchTower
        return {}

    arguments = {
        "pipeline_name": state["pipeline_name"],
        "event_timestamp": state["start_time"],
    }

    db_factory = ctx.db_factory
    redis = ctx.redis

    try:
        async with db_factory() as db:
            gateway = RBACGateway(
                db=db,
                redis=redis,
                investigation_id=state["investigation_id"],
                infra_params=infra_params(state),
            )
            result = await gateway.call(
                tool_name=tool_name,
                arguments=arguments,
                actor="system",
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=platform,
            )
    except Exception:
        # Non-fatal: classifier falls back to the WatchTower-provided error_detail
        logger.exception(
            "initial_evidence_fetch failed — using WatchTower error_detail: investigation_id=%s",
            state["investigation_id"],
        )
        return {}

    if result.get("error"):
        # ADF returned an error (e.g. no_failed_run_found) — keep WatchTower's data
        return {}

    # Enrich error_detail with the full nested execution path and leaf failure.
    # Shape returned by get_activity_run_error:
    # {
    #   "run_id": "...",
    #   "execution_path": [
    #     {"pipeline_name": "Master", "run_id": "...", "activity_name": "Execute B",
    #      "activity_type": "ExecutePipeline", "error": null},
    #     {"pipeline_name": "Pipeline B", ..., "activity_type": "ExecutePipeline", ...},
    #     {"pipeline_name": "Pipeline C", "activity_name": "CopyToSilver",
    #      "activity_type": "Copy", "error": {...}}
    #   ],
    #   "depth": 3,
    #   "leaf": {
    #     "pipeline_name": "Pipeline C", "run_id": "...",
    #     "activity_name": "CopyToSilver", "activity_type": "Copy",
    #     "error_code": "UserErrorInvalidCredentials", "message": "..."
    #   }
    # }
    leaf = result.get("leaf") or {}
    failed_branches = result.get("failed_branches", [])
    enriched: dict = {
        "error_code": leaf.get("error_code"),
        "message": leaf.get("message"),
        "failed_activity_name": leaf.get("activity_name"),
        "failed_activity_type": leaf.get("activity_type"),
        "failed_pipeline_name": leaf.get("pipeline_name"),
        "failed_activity_run_id": leaf.get("run_id"),
        "execution_path": result.get("execution_path", []),
        "depth": result.get("depth", 1),
        "master_run_id": result.get("run_id"),
        # All parallel failed branches — present only when multiple activities failed at once
        "parallel_failure_count": len(failed_branches),
        "failed_branches": [
            {"execution_path": b.get("execution_path", []), "leaf": b.get("leaf")}
            for b in failed_branches[1:]  # skip primary (already in execution_path/leaf above)
        ] if len(failed_branches) > 1 else [],
    }
    return {"error_detail": enriched}
