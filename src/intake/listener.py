import hashlib
import hmac
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy import select

from chat.seed_message import extract_error_code
from chat.thread_setup import create_thread_and_notify
from config.error_categories import categorize_error_code
from config.settings import settings
from db import rca as rca_db
from db.models import Credential, FailureEvent, ProjectMetadata

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events")

_SIGNATURE_HEADER = "X-Radar-Signature-256"


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
    start_time: datetime
    end_time: datetime | None = None
    trigger_type: str | None = None
    last_error: str | None = None
    error_detail: ErrorDetail | None = None
    failure_count: int = 0
    # No `credentials` block anymore — WatchTower only needs to send `project` now.
    # RADAR resolves client_secret itself, per tool call, straight from WatchTower's own
    # public."Credential" table (see gateway/credential_resolution.py). Non-secret ADF infra
    # params come from RADAR's own `credentials` table (onboarding-time config), not the
    # event payload.
    #
    # No `hmac_token` body field either (fixed 2026-07-24) — the signature moved to the
    # `X-Radar-Signature-256` header (see _verify_hmac). The old body-field scheme was
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


async def _resolve_factory_name(db, project: str) -> str | None:
    """A project has exactly one Credential row today (see Credential's own docstring), so the
    first match is the right one — same assumption llm/investigation_state.py's get_credential
    already makes for the same table."""
    result = await db.execute(select(Credential.factory_name).where(Credential.project == project))
    return result.scalar_one_or_none()


@router.post("/pipeline-failure", status_code=202)
async def receive_pipeline_failure(
    request: Request,
    event: PipelineFailureEvent,
    x_radar_signature_256: str | None = Header(default=None, alias=_SIGNATURE_HEADER),
):
    body = await request.body()
    if not x_radar_signature_256 or not _verify_hmac(body, x_radar_signature_256, settings.hmac_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing signature")

    project = _normalize_project(event.project)
    await _ensure_project_metadata(request.app.state.db_factory, project)

    # Batch detection dropped from scope (2026-07-24, user's explicit call) — every failure
    # gets its own investigation + chat entry point unconditionally now. Under the chat-first
    # model, a suppressed batch had no thread a human could actually open to investigate it
    # (_fire_batch_alert only logged a message). src/intake/batch_detection.py itself
    # (check_batch/get_batch_members) stays in the repo unused, for potential future
    # re-enablement once a chat-compatible batch UX is actually designed.

    investigation_id = str(uuid.uuid4())
    error_detail = event.error_detail.model_dump(exclude_none=True) if event.error_detail else None

    async with request.app.state.db_factory() as db:
        factory_name = await _resolve_factory_name(db, project)
        db.add(FailureEvent(
            investigation_id=investigation_id,
            project=project,
            platform=event.platform,
            pipeline_name=event.pipeline_name,
            factory_name=factory_name,
            run_status=event.run_status,
            start_time=event.start_time,
            end_time=event.end_time,
            last_error=event.last_error,
            error_detail=error_detail,
            trigger_type=event.trigger_type,
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    # A cancelled run isn't a real failure — no RCA logging, no seed message, no notification.
    # Restores the old pre_check() short-circuit (workflow/nodes/pre_check.py, deleted along
    # with the retired diagnosis pipeline and never re-implemented here until now).
    if event.run_status in ("Cancelled", "Cancelling", "Canceling"):
        return {"accepted": True, "investigation_id": investigation_id, "user_ids": []}

    # Write the seed message BEFORE record_diagnosis_outcome below — create_thread_and_notify's
    # own matching_rca lookup must see this error's failure_count as it stood BEFORE the current
    # occurrence increments it (2026-08-06 fix: this used to run after, so a first-ever
    # occurrence read back failure_count=1 and rendered "seen 1 time(s) before" instead of 0).
    recipient_user_ids = await create_thread_and_notify(
        db_factory=request.app.state.db_factory,
        investigation_id=investigation_id,
        project=project,
        platform=event.platform,
        pipeline_name=event.pipeline_name,
        run_status=event.run_status,
        last_error=event.last_error,
        error_detail=error_detail,
    )

    # Logs every failure into ProjectRCA immediately, not just ones a human eventually chats
    # about (2026-08-06) — find-or-create keyed by (pipeline_id, project, error_signature),
    # same mechanism record_diagnosis_outcome already used from chat: a repeat of the exact
    # same error_code bumps the existing row's failure_count/last_failure_timestamp instead
    # of creating a duplicate. error_category is only a best-effort guess at this point
    # (categorize_error_code, no LLM involved) — record_diagnosis_outcome unconditionally
    # overwrites it with the real category the first time someone actually diagnoses this in
    # chat, and error_signature is deliberately the raw error_code so that later diagnosis
    # consolidates onto this SAME row rather than fragmenting into a second one (see that
    # tool's own docstring in llm/tools.py, updated to prefer the same key).
    error_code = extract_error_code(error_detail)
    if error_code:
        async with request.app.state.db_factory() as db:
            await rca_db.record_diagnosis_outcome(
                db,
                pipeline_id=event.pipeline_name,
                project=project,
                error_signature=error_code,
                error_category=categorize_error_code(error_code),
            )
            await db.commit()

    return {
        "accepted": True,
        "investigation_id": investigation_id,
        "user_ids": recipient_user_ids,
    }
