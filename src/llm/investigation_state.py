"""
InvestigationState — the shape every chat turn needs, and the only code that builds it.

Rebuilt fresh from DB rows every turn (cheap, all indexed lookups).
"""

from datetime import datetime
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ChatThread, Credential, FailureEvent, ProjectMetadata


class InvestigationState(TypedDict):
    investigation_id: str | None
    # The real ChatThread.thread_id, passed explicitly to RBACGateway/AuditLog — every chat
    # call needs its own real thread_id on the AuditLog row.
    thread_id: str | None
    project: str
    platform: str
    pipeline_name: str
    run_status: str
    start_time: datetime
    end_time: datetime | None
    last_error: str | None
    error_detail: (
        dict | None
    )  # raw WatchTower payload; the chat agent enriches on demand via its own tool calls
    trigger_type: str | None
    thread_status: str  # "running" | "paused" | "failed" | "completed"
    # --- non-secret factory identifiers, sourced from Credential (not a secret,
    #     no Key Vault fetch needed — plain columns, safe in state) ---
    tenant_id: str | None
    client_id: str | None
    subscription_id: str | None
    resource_group: str | None
    factory_name: str | None
    # --- set at intake-time notification (chat/thread_setup, called from intake/listener.py) ---
    notify_sent: bool | None


# Sentinel pipeline_name for ad-hoc threads — RBACGateway.call()/AuditLog both require a
# non-null pipeline_id; there's no real pipeline for a project-scoped, not-failure-specific
# conversation. Chosen to be visually distinct from any real ADF pipeline name.
AD_HOC_PIPELINE_SENTINEL = "(ad-hoc)"


def build_initial_state(
    inv: FailureEvent, factory: Credential, thread_id: str | None = None
) -> InvestigationState:
    return {
        "investigation_id": inv.investigation_id,
        "thread_id": thread_id,
        "project": inv.project,
        "platform": inv.platform,
        "pipeline_name": inv.pipeline_name,
        "run_status": inv.run_status,
        "start_time": inv.start_time,
        "end_time": inv.end_time,
        "last_error": inv.last_error,
        "error_detail": inv.error_detail,
        "trigger_type": inv.trigger_type,
        "thread_status": "running",
        "tenant_id": factory.tenant_id,
        "client_id": factory.client_id,
        "subscription_id": factory.subscription_id,
        "resource_group": factory.resource_group,
        "factory_name": factory.factory_name,
        "notify_sent": None,
    }


async def get_credential(db, project: str) -> Credential | None:
    """A project has exactly one platform instance, so the first matching row is the right one."""
    result = await db.execute(select(Credential).where(Credential.project == project))
    return result.scalars().first()


async def build_chat_state(db: AsyncSession, thread: ChatThread) -> InvestigationState:
    if thread.investigation_id is not None:
        return await _build_failure_triggered_state(db, thread)
    return await _build_ad_hoc_state(db, thread)


async def _build_failure_triggered_state(
    db: AsyncSession, thread: ChatThread
) -> InvestigationState:
    inv = await db.get(FailureEvent, thread.investigation_id)
    if inv is None:
        raise RuntimeError(
            f"ChatThread {thread.thread_id} points at a missing FailureEvent"
        )
    factory = await get_credential(db, inv.project)
    if factory is None:
        raise RuntimeError(f"credentials row missing for project='{inv.project}'")

    return build_initial_state(inv, factory, thread_id=thread.thread_id)


async def _build_ad_hoc_state(
    db: AsyncSession, thread: ChatThread
) -> InvestigationState:
    """
    No FailureEvent to source failure-specific fields from — pipeline_name/run_status/
    error_detail etc. are deliberately absent/sentinel. llm/agent.py's own system-prompt
    builder must tolerate this shape.
    """
    factory = await get_credential(db, thread.project)
    if factory is None:
        raise RuntimeError(f"credentials row missing for project='{thread.project}'")

    platform_result = await db.execute(
        select(ProjectMetadata.platform).where(
            ProjectMetadata.project == thread.project
        )
    )
    platform = platform_result.scalar_one_or_none()

    return {
        "investigation_id": None,
        "thread_id": thread.thread_id,
        "project": thread.project,
        "platform": platform or "unknown",
        "pipeline_name": AD_HOC_PIPELINE_SENTINEL,
        "run_status": "n/a",
        "start_time": thread.created_at,
        "end_time": None,
        "last_error": None,
        "error_detail": None,
        "trigger_type": None,
        "thread_status": "running",
        "tenant_id": factory.tenant_id,
        "client_id": factory.client_id,
        "subscription_id": factory.subscription_id,
        "resource_group": factory.resource_group,
        "factory_name": factory.factory_name,
        "notify_sent": None,
    }
