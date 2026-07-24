import hashlib
import hmac
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import settings
from db.models import Investigation, ProjectMetadata
from gateway.vault import VaultResolutionError, populate_credentials

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events")

_SIGNATURE_HEADER = "X-Nexus-Signature-256"


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
    # No `credentials` block anymore — WatchTower only needs to send `project` now.
    # Nexus resolves project -> ProjectFactory.key_vault_uri -> secret itself
    # (see gateway/vault.py). Non-secret ADF infra params also come from
    # ProjectMetadata (onboarding-time config), not the event payload.
    #
    # No `hmac_token` body field either (fixed 2026-07-24) — the signature moved to the
    # `X-Nexus-Signature-256` header (see _verify_hmac). The old body-field scheme was
    # self-referentially broken: it signed the full raw body, which necessarily included
    # the signature field's own value — there is no way to correctly compute a signature
    # over a body that already contains that exact signature. A header-based scheme (same
    # shape as GitHub's X-Hub-Signature-256 / Stripe's Stripe-Signature) signs only the
    # body's actual content, so it isn't self-referential.


def _normalize_project(name: str) -> str:
    return re.sub(r'_+', '_', re.sub(r'[^a-z0-9]', '_', name.lower())).strip('_')


def _verify_hmac(payload_bytes: bytes, signature: str, secret: str) -> bool:
    expected = hmac.HMAC(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _ensure_project_metadata(db_factory, project: str) -> None:
    stmt = (
        pg_insert(ProjectMetadata)
        .values(project=project)
        .on_conflict_do_nothing(index_elements=["project"])
    )
    async with db_factory() as db:
        await db.execute(stmt)
        await db.commit()


@router.post("/pipeline-failure", status_code=202)
async def receive_pipeline_failure(
    request: Request,
    event: PipelineFailureEvent,
    x_nexus_signature_256: str | None = Header(default=None, alias=_SIGNATURE_HEADER),
):
    body = await request.body()
    if not x_nexus_signature_256 or not _verify_hmac(body, x_nexus_signature_256, settings.hmac_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing signature")

    project = _normalize_project(event.project)
    await _ensure_project_metadata(request.app.state.db_factory, project)

    # Batch detection dropped from scope (2026-07-24, user's explicit call) — every failure
    # gets its own investigation + chat entry point unconditionally now. Under the chat-first
    # model, a suppressed batch had no thread a human could actually open to investigate it
    # (_fire_batch_alert only logged a message). src/intake/batch_detection.py itself
    # (check_batch/get_batch_members) stays in the repo unused, for potential future
    # re-enablement once a chat-compatible batch UX is actually designed.

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
