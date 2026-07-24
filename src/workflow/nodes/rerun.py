import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from config.settings import settings
from db.models import AuditLog, ProjectRCA
from gateway.rbac import RBACGateway, infra_params
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


def _rerun_outcome_summary(pipeline_name: str, outcome: str, new_run_id: str | None) -> str:
    """
    Audit-log commentary only, never delivered to a human — the person who approved the
    rerun checks the result themselves (explicit call, not an oversight). Lives here rather
    than in notifications/messages.py since nothing about it is actually a notification.
    """
    if outcome == "succeeded":
        body = "The pipeline rerun completed successfully."
        if new_run_id:
            body += f" Run ID: {new_run_id}"
        return body
    if outcome == "failed":
        body = "The pipeline rerun failed again. Manual investigation is required."
        if new_run_id:
            body += f" Run ID: {new_run_id}"
        return body
    return (
        "The pipeline rerun was triggered but its outcome could not be determined within "
        "the monitoring window. Check ADF directly."
    )


async def _check_rerun_outcome(
    new_run_id: str,
    state: dict,
    ctx: WorkflowContext,
) -> None:
    """
    Fire-and-forget coroutine: polls ADF for the rerun outcome, then writes audit log.
    Runs outside any concurrency guard the caller applied to rerun() itself.
    """
    db_factory = ctx.db_factory
    redis = ctx.redis
    approval_actor = ctx.approval_actor or "unknown"

    # Calculate wait interval from the pipeline's own last-run duration.
    # If start_time and end_time are available, use duration * 1.2 (20% buffer).
    # Fall back to the configured default if the times are missing or unparseable.
    wait_seconds: float = settings.rerun_outcome_check_interval_seconds
    retry_seconds: float = settings.rerun_outcome_check_interval_seconds
    try:
        if state.get("start_time") and state.get("end_time"):
            start_dt = datetime.fromisoformat(state["start_time"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(state["end_time"].replace("Z", "+00:00"))
            duration = (end_dt - start_dt).total_seconds()
            if duration > 0:
                wait_seconds = max(60.0, min(7200.0, duration * 1.2))
                retry_seconds = max(60.0, min(3600.0, duration * 0.5))
    except Exception:
        logger.exception("Could not compute wait from pipeline duration — using default")

    outcome = "unknown"
    for attempt in range(settings.rerun_outcome_max_checks):
        await asyncio.sleep(wait_seconds if attempt == 0 else retry_seconds)
        try:
            async with db_factory() as db:
                gateway = RBACGateway(
                    db=db,
                    redis=redis,
                    investigation_id=state["investigation_id"],
                    infra_params=infra_params(state),
                )
                result = await gateway.call(
                    tool_name="get_pipeline_run_status",
                    arguments={"run_id": new_run_id},
                    actor="system",
                    pipeline_id=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                )
            status = result.get("status")
            if status == "Succeeded":
                outcome = "succeeded"
                break
            elif status == "Failed":
                outcome = "failed"
                break
        except Exception:
            logger.exception(
                "Outcome check attempt %d failed: investigation_id=%s",
                attempt + 1,
                state["investigation_id"],
            )

    now = datetime.now(tz=timezone.utc)

    # Update RCA row and write audit log
    rca_id = state.get("rca_id")
    if rca_id is not None:
        try:
            async with db_factory() as db:
                rca_result = await db.execute(select(ProjectRCA).where(ProjectRCA.id == rca_id))
                rca = rca_result.scalar_one_or_none()
                if rca is not None and rca.last_rerun_attempt:
                    rca.last_rerun_attempt = {
                        **rca.last_rerun_attempt,
                        "cleared_by_successful_run": (outcome == "succeeded"),
                    }
                    db.add(AuditLog(
                        investigation_id=state["investigation_id"],
                        pipeline_id=state["pipeline_name"],
                        project=state["project"],
                        platform=state["platform"],
                        timestamp=now,
                        event_type=f"rerun_{outcome}",
                        actor=approval_actor,
                        detail={
                            "new_run_id": new_run_id,
                            "outcome": outcome,
                            "body": _rerun_outcome_summary(state["pipeline_name"], outcome, new_run_id),
                        },
                    ))
                    await db.commit()
        except Exception:
            logger.exception("Failed to update RCA outcome: investigation_id=%s", state["investigation_id"])


async def rerun(state: InvestigationState, ctx: WorkflowContext) -> dict:
    db_factory = ctx.db_factory
    redis = ctx.redis
    approval_actor: str = ctx.approval_actor or "unknown"

    # Idempotency guard — prevent duplicate reruns from a double-click
    idempotency_key = f"rerun_triggered:{state['investigation_id']}"
    was_set = await redis.set(idempotency_key, "1", nx=True, ex=3600)
    if not was_set:
        async with db_factory() as db:
            db.add(AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(timezone.utc),
                event_type="rerun_duplicate_skipped",
                actor=approval_actor,
                detail={"reason": "idempotency_key_already_set"},
            ))
            await db.commit()
        return {"thread_status": "completed"}

    # No role gate here — approval_actor already claimed this thread (chat_threads.
    # claimed_by_user_email) and clicked approve on the native consent dialog; that IS the
    # authorization. RBACGateway.call() below still independently checks rbac_permissions
    # for a per-tool allowed/requires_consent gate, just with no role dimension anymore.

    # Write rerun_approved audit log
    async with db_factory() as db:
        db.add(AuditLog(
            investigation_id=state["investigation_id"],
            pipeline_id=state["pipeline_name"],
            project=state["project"],
            platform=state["platform"],
            timestamp=datetime.now(timezone.utc),
            event_type="rerun_approved",
            actor=approval_actor,
            detail={"rca_id": state.get("rca_id")},
        ))
        await db.commit()

    # Freshness check — only call ADF if enough time has elapsed since the fix was proposed
    proposed_at_str = state.get("proposed_at")
    should_check_freshness = True
    if proposed_at_str:
        elapsed = (
            datetime.now(tz=timezone.utc) - datetime.fromisoformat(proposed_at_str)
        ).total_seconds()
        should_check_freshness = elapsed > settings.rerun_freshness_window_seconds

    if should_check_freshness:
        try:
            async with db_factory() as db:
                gateway = RBACGateway(
                    db=db,
                    redis=redis,
                    investigation_id=state["investigation_id"],
                    infra_params=infra_params(state),
                )
                history = await gateway.call(
                    tool_name="get_pipeline_run_history",
                    arguments={"pipeline_name": state["pipeline_name"], "days": 1},
                    actor=approval_actor,
                    pipeline_id=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                )
            runs = history.get("runs", [])
            already_running = any(
                r.get("status") in ("InProgress", "Queued") or
                (r.get("status") == "Succeeded" and r.get("start", "") > state["start_time"])
                for r in runs
            )
            if already_running:
                async with db_factory() as db:
                    db.add(AuditLog(
                        investigation_id=state["investigation_id"],
                        pipeline_id=state["pipeline_name"],
                        project=state["project"],
                        platform=state["platform"],
                        timestamp=datetime.now(timezone.utc),
                        event_type="rerun_already_resolved_externally",
                        actor=approval_actor,
                        detail={"reason": "already_recovered_or_running"},
                    ))
                    await db.commit()
                return {"thread_status": "completed"}
        except Exception:
            logger.exception(
                "Freshness check failed — proceeding with rerun: investigation_id=%s",
                state["investigation_id"],
            )

    # Execute rerun via RBAC gateway
    rerun_outcome: str
    rerun_detail: dict = {}
    new_run_id: str | None = None
    try:
        async with db_factory() as db:
            gateway = RBACGateway(
                db=db,
                redis=redis,
                investigation_id=state["investigation_id"],
                infra_params=infra_params(state),
            )
            result = await gateway.call(
                tool_name="rerun_pipeline",
                arguments={"pipeline_name": state["pipeline_name"]},
                actor=approval_actor,
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
            )
        rerun_outcome = "triggered"
        new_run_id = result.get("new_run_id")
        rerun_detail = {"new_run_id": new_run_id}
    except PermissionError:
        rerun_outcome = "denied_rbac"
        rerun_detail = {}
    except Exception as exc:
        rerun_outcome = "failed"
        rerun_detail = {"error": str(exc)}
        logger.exception("Rerun failed: investigation_id=%s", state["investigation_id"])

    now = datetime.now(tz=timezone.utc)

    # Record rerun attempt on the RCA row and write audit log
    rca_id = state.get("rca_id")
    if rca_id is not None:
        async with db_factory() as db:
            rca_result = await db.execute(select(ProjectRCA).where(ProjectRCA.id == rca_id))
            rca: ProjectRCA | None = rca_result.scalar_one_or_none()
            if rca is not None:
                rca.last_rerun_attempt = {
                    "timestamp": now.isoformat(),
                    "outcome": rerun_outcome,
                    "actor": approval_actor,
                    "error_category_before_rerun": state.get("error_category"),
                    "cleared_by_successful_run": False,
                    **rerun_detail,
                }
                db.add(AuditLog(
                    investigation_id=state["investigation_id"],
                    pipeline_id=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                    timestamp=now,
                    event_type="rerun_executed",
                    actor=approval_actor,
                    detail={"outcome": rerun_outcome, **rerun_detail},
                ))
                await db.commit()

    # Fire-and-forget outcome check if rerun was triggered successfully
    if rerun_outcome == "triggered" and new_run_id:
        asyncio.create_task(_check_rerun_outcome(new_run_id=new_run_id, state=dict(state), ctx=ctx))

    return {"thread_status": "completed"}
