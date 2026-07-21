import asyncio
import hashlib
import hmac
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import settings
from db.models import Investigation, ProjectMetadata
from gateway.vault import VaultResolutionError, populate_credentials
from intake.batch_detection import check_batch, get_batch_members
from notifications import messages

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events")


class ErrorDetail(BaseModel):
    error_code: str | None = None
    message: str | None = None
    failed_activity_name: str | None = None
    failed_activity_run_id: str | None = None


class PipelineFailureEvent(BaseModel):
    project: str
    platform: str
    pipeline_name: str
    run_status: str
    start_time: str
    end_time: str | None = None
    trigger_type: str | None = None
    last_error: str | None = None
    error_detail: ErrorDetail | None = None
    failure_count: int = 0
    hmac_token: str
    # No `credentials` block anymore — WatchTower only needs to send `project` now.
    # Nexus resolves project -> ProjectMetadata.key_vault_uri -> secret itself
    # (see gateway/vault.py). Non-secret ADF infra params also come from
    # ProjectMetadata (onboarding-time config), not the event payload.


def _normalize_project(name: str) -> str:
    return re.sub(r'_+', '_', re.sub(r'[^a-z0-9]', '_', name.lower())).strip('_')


def _verify_hmac(payload_bytes: bytes, token: str, secret: str) -> bool:
    expected = hmac.HMAC(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token)


async def _ensure_project_metadata(db_factory, project: str) -> None:
    stmt = (
        pg_insert(ProjectMetadata)
        .values(project=project)
        .on_conflict_do_nothing(index_elements=["project"])
    )
    async with db_factory() as db:
        await db.execute(stmt)
        await db.commit()


async def _fire_batch_alert(request: Request, event: PipelineFailureEvent) -> None:
    now = time.time()
    members = await get_batch_members(request.app.state.redis, event.platform, now)
    # Members are "project:timestamp" strings — extract unique project names
    recent_projects = list({m.rsplit(":", 1)[0] for m in members})
    window_minutes = settings.batch_window_seconds // 60

    body = messages.batch_alert_body(event.platform, len(members), window_minutes, recent_projects)
    # Delivery (Outlook digest email) is built in the next milestone — logged for now.
    logger.info("Batch alert: platform=%s %s", event.platform, body)


@router.post("/pipeline-failure", status_code=202)
async def receive_pipeline_failure(request: Request, event: PipelineFailureEvent):
    body = await request.body()
    if settings.hmac_secret is not None and not _verify_hmac(body, event.hmac_token, settings.hmac_secret):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    project = _normalize_project(event.project)
    await _ensure_project_metadata(request.app.state.db_factory, project)

    # Batch detection — check before creating investigation_id or touching Redis creds
    now = time.time()
    batch_status = await check_batch(
        redis=request.app.state.redis,
        platform=event.platform,
        project=project,
        now=now,
    )

    if batch_status == "batch_alert":
        # This is the threshold-th failure — send one aggregated alert, suppress investigation
        asyncio.create_task(_fire_batch_alert(request, event))
        logger.info(
            "Batch threshold reached: platform=%s project=%s — individual investigation suppressed",
            event.platform,
            project,
        )
        return {"accepted": True, "status": "batch_alert_sent"}

    if batch_status == "batch_suppress":
        # Already alerted for this batch — silently suppress
        logger.info(
            "Batch suppression: platform=%s project=%s",
            event.platform,
            project,
        )
        return {"accepted": True, "status": "batch_suppressed"}

    # Individual investigation — record the failure, but do NOT auto-invoke diagnosis.
    # Diagnosis is deferred to a live "Diagnose" click (next milestone's chat endpoint).
    investigation_id = str(uuid.uuid4())

    try:
        await populate_credentials(
            db_factory=request.app.state.db_factory,
            redis=request.app.state.redis,
            project=project,
            investigation_id=investigation_id,
        )
    except VaultResolutionError:
        logger.exception(
            "Failed to populate credentials from Key Vault: project=%s investigation_id=%s",
            project, investigation_id,
        )
        raise HTTPException(status_code=500, detail="Failed to resolve project credentials")

    async with request.app.state.db_factory() as db:
        db.add(Investigation(
            investigation_id=investigation_id,
            project=project,
            platform=event.platform,
            pipeline_name=event.pipeline_name,
            run_status=event.run_status,
            start_time=event.start_time,
            end_time=event.end_time,
            last_error=event.last_error,
            error_detail=event.error_detail.model_dump(exclude_none=True) if event.error_detail else None,
            failure_count=event.failure_count,
            trigger_type=event.trigger_type,
            status="pending_diagnosis",
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    return {"accepted": True, "investigation_id": investigation_id}
