import logging

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import Investigation, ProjectFactory
from gateway.concurrency import DistributedSemaphore
from workflow.context import WorkflowContext
from workflow.nodes.initial_evidence_fetch import initial_evidence_fetch
from workflow.nodes.investigator import investigator
from workflow.nodes.load_context import load_context
from workflow.nodes.notifier import notifier
from workflow.nodes.pre_check import pre_check
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)


def _build_initial_state(inv: Investigation, factory: ProjectFactory) -> InvestigationState:
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
        "tenant_id": factory.tenant_id,
        "client_id": factory.client_id,
        "subscription_id": factory.subscription_id,
        "resource_group": factory.resource_group,
        "factory_name": factory.factory_name,
        "error_category": None,
        "prior_rca_context": None,
        "rca_id": None,
        "investigation_summary": None,
        "requires_human_action": None,
        "notify_sent": None,
        "needs_approval": None,
        "approval_action": None,
        "proposed_at": None,
    }


_RESULT_FIELDS = (
    "error_category", "prior_rca_context", "requires_human_action",
    "rca_id", "investigation_summary", "notify_sent", "needs_approval", "proposed_at",
)


async def run_diagnosis(
    investigation_id: str,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    semaphore: DistributedSemaphore | None = None,
) -> InvestigationState:
    """
    Plain, directly-callable diagnosis pipeline — invoked once a human clicks "Diagnose" in
    the chat UI (later milestone), not automatically at intake. Cancellation is the only
    pre-chat branch; known-fix reuse and cascade detection are tools the live investigator
    agent calls itself (check_known_fix/check_upstream_dependencies), not separate routing
    steps, so anything that isn't cancelled goes straight to the full investigator:

      pre_check -> (cancelled short-circuit) -> notifier
        -> (not cancelled) -> load_context -> investigator -> notifier
    """
    async def _run() -> InvestigationState:
        async with db_factory() as db:
            inv = await db.get(Investigation, investigation_id)
            if inv is None:
                raise ValueError(f"No investigation found for investigation_id={investigation_id}")
            factory = await _get_project_factory(db, inv.project)
            if factory is None:
                raise RuntimeError(
                    f"project_factories row missing for project='{inv.project}' — "
                    "onboarding may not have configured this project's platform yet. Check logs."
                )

        state = _build_initial_state(inv, factory)
        ctx = WorkflowContext(db_factory=db_factory, redis=redis)

        try:
            state.update(await initial_evidence_fetch(state, ctx))
            state.update(await pre_check(state, ctx))

            if state["error_category"] != "cancelled":
                state.update(await load_context(state, ctx))
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
        factory = await _get_project_factory(db, inv.project)
        if factory is None:
            raise RuntimeError(f"project_factories row missing for project='{inv.project}'")

    state = _build_initial_state(inv, factory)
    state.update(inv.diagnosis_result)
    return state


async def _get_project_factory(db, project: str) -> ProjectFactory | None:
    """
    Current scope (2026-07-24): a project has exactly one platform instance, so the first
    matching row is the right one. Revisit once multi-factory-per-project is actually built.
    """
    result = await db.execute(select(ProjectFactory).where(ProjectFactory.project == project))
    return result.scalars().first()
