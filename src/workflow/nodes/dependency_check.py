import logging
from datetime import datetime, timezone

from db.models import AuditLog
from gateway.rbac import RBACGateway, adf_infra_params
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


async def dependency_check(state: InvestigationState, ctx: WorkflowContext) -> dict:
    """
    Cascade detection node. Reached from:
      - pre_check no-history path (brand-new pipeline)
      - classifier bucket=3 (new/ambiguous error)

    Strategy:
      1. Call get_pipeline_definition on the failed pipeline to auto-discover
         any ExecutePipeline activities (child pipelines this one calls).
         These are within-ADF orchestration dependencies.
      2. For each discovered upstream, check if it also failed recently.
         If yes → cascade confirmed → route to notifier (no investigation needed).
    """
    db_factory = ctx.db_factory
    redis = ctx.redis

    async def _make_gateway(db):
        return RBACGateway(
            db=db,
            redis=redis,
            investigation_id=state["investigation_id"],
            infra_params=adf_infra_params(state),
        )

    # --- Step 1: Auto-discover ExecutePipeline references from pipeline definition ---
    discovered_upstreams: list[str] = []
    try:
        async with db_factory() as db:
            gateway = await _make_gateway(db)
            definition = await gateway.call(
                tool_name="get_pipeline_definition",
                arguments={"pipeline_name": state["pipeline_name"]},
                actor="system",
                role="investigator",
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
            )
        discovered_upstreams = [
            a["references_pipeline"]
            for a in definition.get("activities", [])
            if a.get("type") == "ExecutePipeline" and a.get("references_pipeline")
        ]
    except Exception:
        logger.warning(
            "get_pipeline_definition failed in dependency_check — proceeding without auto-discovery: "
            "investigation_id=%s",
            state["investigation_id"],
        )

    all_upstreams = list(dict.fromkeys(discovered_upstreams))  # preserves order, dedupes

    if not all_upstreams:
        async with db_factory() as db:
            db.add(AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(tz=timezone.utc),
                event_type="dependency_validation_outcome",
                actor="dependency_check",
                detail={"outcome": "no_upstream_declared", "proceed": "investigator"},
            ))
            await db.commit()
        return {"upstream_also_failed": None, "classification_bucket": 3}

    # --- Step 3: Check each upstream for recent failures ---
    upstream_that_failed: str | None = None
    for upstream_pipeline in all_upstreams:
        try:
            async with db_factory() as db:
                gateway = await _make_gateway(db)
                result = await gateway.call(
                    tool_name="get_pipeline_run_history",
                    arguments={"pipeline_name": upstream_pipeline, "days": 1},
                    actor="system",
                    role="investigator",
                    pipeline_id=upstream_pipeline,
                    project=state["project"],
                    platform=state["platform"],
                )
            runs = result.get("runs", [])
            if any(
                r.get("status") == "Failed" and r.get("start", "") <= state["start_time"]
                for r in runs
            ):
                upstream_that_failed = upstream_pipeline
                break
        except Exception:
            logger.warning(
                "Failed to check run history for upstream=%s investigation_id=%s",
                upstream_pipeline,
                state["investigation_id"],
            )

    upstream_also_failed = upstream_that_failed is not None
    outcome = "direct_attribution" if upstream_also_failed else "correlation_ruled_out"

    async with db_factory() as db:
        db.add(AuditLog(
            investigation_id=state["investigation_id"],
            pipeline_id=state["pipeline_name"],
            project=state["project"],
            platform=state["platform"],
            timestamp=datetime.now(tz=timezone.utc),
            event_type="dependency_validation_outcome",
            actor="dependency_check",
            detail={
                "outcome": outcome,
                "discovered_upstreams": discovered_upstreams,
                "upstream_that_failed": upstream_that_failed,
                "all_upstreams_checked": all_upstreams,
            },
        ))
        await db.commit()

    return {
        "upstream_also_failed": upstream_also_failed,
        "classification_bucket": 2 if upstream_also_failed else 3,
    }
