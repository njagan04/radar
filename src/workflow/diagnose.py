import asyncio
import logging

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import Investigation, ProjectMetadata
from workflow.context import WorkflowContext
from workflow.nodes.classifier import classifier
from workflow.nodes.dependency_check import dependency_check
from workflow.nodes.initial_evidence_fetch import initial_evidence_fetch
from workflow.nodes.investigator import investigator
from workflow.nodes.load_context import load_context
from workflow.nodes.notifier import notifier
from workflow.nodes.pre_check import pre_check
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


def _build_initial_state(inv: Investigation, pm: ProjectMetadata) -> InvestigationState:
    return {
        "investigation_id": inv.investigation_id,
        "project": inv.project,
        "platform": inv.platform,
        "pipeline_name": inv.pipeline_name,
        "run_status": inv.run_status,
        "start_time": inv.start_time,
        "end_time": inv.end_time,
        "last_error": inv.last_error,
        "error_detail": inv.error_detail,
        "failure_count": inv.failure_count,
        "trigger_type": inv.trigger_type,
        "thread_status": "running",
        "adf_tenant_id": pm.adf_tenant_id,
        "adf_client_id": pm.adf_client_id,
        "adf_subscription_id": pm.adf_subscription_id,
        "adf_resource_group": pm.adf_resource_group,
        "adf_factory_name": pm.adf_factory_name,
        "has_prior_history": None,
        "classification_bucket": None,
        "classification_reasoning": None,
        "error_category": None,
        "known_fix": None,
        "requires_human_action": None,
        "cross_pipeline_match": None,
        "cross_pipeline_source": None,
        "upstream_also_failed": None,
        "rca_id": None,
        "investigation_summary": None,
        "notify_sent": None,
        "needs_approval": None,
        "approval_action": None,
        "proposed_at": None,
    }


_RESULT_FIELDS = (
    "classification_bucket", "classification_reasoning", "error_category", "known_fix",
    "requires_human_action", "cross_pipeline_match", "cross_pipeline_source",
    "upstream_also_failed", "rca_id", "investigation_summary", "notify_sent",
    "needs_approval", "proposed_at",
)


async def run_diagnosis(
    investigation_id: str,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    semaphore: asyncio.BoundedSemaphore | None = None,
) -> InvestigationState:
    """
    Plain, directly-callable replacement for the old LangGraph `graph.ainvoke(...)` —
    invoked once a human clicks "Diagnose" in the chat UI (next milestone), not
    automatically at intake. Runs the same node sequence the graph used to route through,
    just as ordinary if-statements instead of conditional graph edges:

      pre_check -> (cancelled short-circuit) -> classifier
        -> bucket 0/1 or requires_human_action -> notifier
        -> bucket 3 -> dependency_check -> (cascade: notifier) | (no cascade: investigator -> notifier)
    """
    async def _run() -> InvestigationState:
        async with db_factory() as db:
            inv = await db.get(Investigation, investigation_id)
            if inv is None:
                raise ValueError(f"No investigation found for investigation_id={investigation_id}")
            pm = await db.get(ProjectMetadata, inv.project)
            if pm is None:
                raise RuntimeError(
                    f"project_metadata row missing for project='{inv.project}' — "
                    "intake upsert may have failed. Check logs."
                )

        state = _build_initial_state(inv, pm)
        ctx = WorkflowContext(db_factory=db_factory, redis=redis)

        try:
            state.update(await initial_evidence_fetch(state, ctx))
            state.update(await load_context(state, ctx))
            state.update(await pre_check(state, ctx))

            if not (state["classification_bucket"] == 0 and state["error_category"] == "cancelled"):
                state.update(await classifier(state, ctx))

                if not state.get("requires_human_action") and state["classification_bucket"] not in (0, 1):
                    state.update(await dependency_check(state, ctx))
                    if not state.get("upstream_also_failed"):
                        state.update(await investigator(state, ctx))

            state.update(await notifier(state, ctx))
        except Exception:
            logger.exception(
                "Diagnosis failed: investigation_id=%s project=%s pipeline=%s",
                investigation_id, inv.project, inv.pipeline_name,
            )
            raise

        async with db_factory() as db:
            row = await db.get(Investigation, investigation_id)
            row.status = "diagnosed"
            row.diagnosis_result = {k: state.get(k) for k in _RESULT_FIELDS}
            await db.commit()

        return state

    if semaphore is not None:
        async with semaphore:
            return await _run()
    return await _run()


async def apply_rerun(investigation_id: str, db_factory: async_sessionmaker, redis: aioredis.Redis, approval_actor: str) -> dict:
    """Directly-callable replacement for the graph's approve->rerun edge — the next
    milestone's chat "approve" click will call this. Reconstructs state from the persisted
    Investigation row + its stored diagnosis_result."""
    from workflow.nodes.rerun import rerun

    state = await _load_diagnosed_state(investigation_id, db_factory)
    ctx = WorkflowContext(db_factory=db_factory, redis=redis, approval_actor=approval_actor)
    return await rerun(state, ctx)


async def apply_denial(investigation_id: str, db_factory: async_sessionmaker, redis: aioredis.Redis, approval_actor: str) -> dict:
    """Directly-callable replacement for the graph's deny->handle_denial edge."""
    from workflow.nodes.handle_denial import handle_denial

    state = await _load_diagnosed_state(investigation_id, db_factory)
    ctx = WorkflowContext(db_factory=db_factory, redis=redis, approval_actor=approval_actor)
    return await handle_denial(state, ctx)


async def _load_diagnosed_state(investigation_id: str, db_factory: async_sessionmaker) -> InvestigationState:
    async with db_factory() as db:
        inv = await db.get(Investigation, investigation_id)
        if inv is None:
            raise ValueError(f"No investigation found for investigation_id={investigation_id}")
        if inv.status != "diagnosed" or inv.diagnosis_result is None:
            raise RuntimeError(
                f"investigation_id={investigation_id} has not completed diagnosis yet "
                f"(status={inv.status!r})"
            )
        pm = await db.get(ProjectMetadata, inv.project)
        if pm is None:
            raise RuntimeError(f"project_metadata row missing for project='{inv.project}'")

    state = _build_initial_state(inv, pm)
    state.update(inv.diagnosis_result)
    return state
