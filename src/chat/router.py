"""
Thin FastAPI layer over chat/service.py — parses requests, calls service functions, returns
JSON. Reads request.app.state.db_factory/redis directly (matching intake/listener.py's
convention); Depends() is used only for get_current_user_id, so there's exactly one new
DI pattern introduced here, not two competing styles.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from chat import service
from chat.access import require_thread_access
from chat.deps import get_current_user_id
from chat.summarization import count_tokens
from db.models import ChatThread
from llm.agent import cancel_chat_stream

router = APIRouter(prefix="/chat")


class CreateThreadRequest(BaseModel):
    project: str
    investigation_id: str | None = None


class MessageRequest(BaseModel):
    content: str


class ToolApprovalResolveRequest(BaseModel):
    decision: str  # "approve" | "deny"
    tool_call_id: str | None = None
    rejection_message: str | None = None


class RenameThreadRequest(BaseModel):
    title: str


class FeedbackRequest(BaseModel):
    rating: str  # "up" | "down"


def _message_out(m) -> dict:
    return {
        "id": m.id,
        "thread_id": m.thread_id,
        "role": m.role,
        "content": m.content,
        "tool_calls": m.tool_calls,
        "created_at": m.created_at.isoformat(),
        "token_count": count_tokens(m.content),
    }


def _thread_out(t) -> dict:
    # pending_tools surfaces a stuck approval (e.g. the user navigated away before resolving
    # it) so the frontend can rebuild the approval card on load instead of the thread being
    # silently blocked from new messages (prepare_message_send 409s while this is set).
    pending = t.pending_tool_approval
    return {
        "thread_id": t.thread_id,
        "project": t.project,
        "investigation_id": t.investigation_id,
        "title": t.title,
        "claimed_by_user_id": t.claimed_by_user_id,
        "created_at": t.created_at.isoformat(),
        "pending_tools": pending.get("pending_tools", []) if pending else [],
    }


def _notification_out(failure_event) -> dict:
    return {
        "investigation_id": failure_event.investigation_id,
        "project": failure_event.project,
        "pipeline_name": failure_event.pipeline_name,
        "seed_message": failure_event.seed_message,
        "resolved": failure_event.resolved_thread_id is not None,
        "resolved_thread_id": failure_event.resolved_thread_id,
        "seen_at": failure_event.seen_at.isoformat() if failure_event.seen_at else None,
        "created_at": failure_event.created_at.isoformat(),
    }


@router.get("/notifications")
async def list_notifications(
    request: Request, project: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        failure_events = await service.list_notifications(db, user_id, project)
        return {"notifications": [_notification_out(f) for f in failure_events]}


@router.post("/notifications/{investigation_id}/seen")
async def mark_notification_seen(
    request: Request, investigation_id: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        failure_event = await service.mark_notification_seen(
            db, user_id, investigation_id
        )
        return {"notification": _notification_out(failure_event)}


@router.get("/notifications/{investigation_id}")
async def get_notification(
    request: Request, investigation_id: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        failure_event = await service.get_pending_notification(
            db, user_id, investigation_id
        )
        if failure_event is None:
            return {"notification": None}
        return {"notification": _notification_out(failure_event)}


@router.get("/threads")
async def list_threads(
    request: Request, project: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        threads = await service.list_threads(db, user_id, project)
        return {"threads": [_thread_out(t) for t in threads]}


@router.post("/threads")
async def create_thread(
    request: Request,
    body: CreateThreadRequest,
    user_id: str = Depends(get_current_user_id),
):
    async with request.app.state.db_factory() as db:
        thread = await service.create_ad_hoc_thread(
            db, user_id, body.project, body.investigation_id
        )
        return _thread_out(thread)


@router.get("/threads/{thread_id}")
async def get_thread(
    request: Request,
    thread_id: str,
    before_id: int | None = None,
    limit: int = 30,
    user_id: str = Depends(get_current_user_id),
):
    async with request.app.state.db_factory() as db:
        thread, messages, has_more = await service.get_thread_with_messages(
            db, user_id, thread_id, before_id, limit
        )
        return {
            "thread": _thread_out(thread),
            "messages": [_message_out(m) for m in messages],
            "has_more": has_more,
        }


@router.get("/threads/{thread_id}/stats")
async def get_thread_stats(
    request: Request, thread_id: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        stats = await service.get_thread_stats(db, user_id, thread_id)
        notification_created_at = stats["notification_created_at"]
        return {
            **stats,
            "notification_created_at": notification_created_at.isoformat()
            if notification_created_at
            else None,
        }


@router.get("/admin/analytics")
async def get_admin_analytics(
    request: Request, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        return await service.get_admin_analytics(db, user_id)


@router.post("/threads/{thread_id}/claim")
async def claim_thread(
    request: Request, thread_id: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        thread = await service.claim_thread(db, user_id, thread_id)
        return _thread_out(thread)


@router.patch("/threads/{thread_id}")
async def rename_thread(
    request: Request,
    thread_id: str,
    body: RenameThreadRequest,
    user_id: str = Depends(get_current_user_id),
):
    async with request.app.state.db_factory() as db:
        thread = await service.rename_thread(db, user_id, thread_id, body.title)
        return _thread_out(thread)


@router.delete("/threads/{thread_id}")
async def delete_thread(
    request: Request, thread_id: str, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        await service.delete_thread(db, user_id, thread_id)
        return {"deleted": True}


def _outcome_response(response: Response, outcome) -> dict:
    """Shared by post_message and resolve_tool_approval — branches on PostMessageOutcome.kind.
    202 (not 200) for a paused turn so clients can distinguish "accepted, but paused" without
    parsing the body."""
    if outcome.kind == "message":
        return _message_out(outcome.message)
    if outcome.kind == "pending_approval":
        response.status_code = 202
        return {"status": "pending_approval", "pending_tools": outcome.pending_tools}
    if outcome.kind == "expired":
        response.status_code = 410
        return {"status": "expired", "detail": outcome.detail}
    raise RuntimeError(f"Unknown PostMessageOutcome.kind={outcome.kind!r}")


@router.post("/threads/{thread_id}/messages")
async def post_message(
    request: Request,
    response: Response,
    thread_id: str,
    body: MessageRequest,
    user_id: str = Depends(get_current_user_id),
):
    db_factory = request.app.state.db_factory
    async with db_factory() as db:
        outcome = await service.post_message(
            db, db_factory, request.app.state.redis, user_id, thread_id, body.content
        )
        return _outcome_response(response, outcome)


@router.post("/threads/{thread_id}/tool-approvals/resolve")
async def resolve_tool_approval(
    request: Request,
    response: Response,
    thread_id: str,
    body: ToolApprovalResolveRequest,
    user_id: str = Depends(get_current_user_id),
):
    db_factory = request.app.state.db_factory
    async with db_factory() as db:
        outcome = await service.resolve_tool_approval(
            db,
            db_factory,
            request.app.state.redis,
            user_id,
            thread_id,
            body.decision,
            body.tool_call_id,
            body.rejection_message,
        )
        return _outcome_response(response, outcome)


def _sse(event: dict) -> bytes:
    if event.get("type") == "message":
        event = {"type": "message", "message": _message_out(event["message"])}
    return f"data: {json.dumps(event, default=str)}\n\n".encode()


@router.post("/threads/{thread_id}/messages/stream")
async def post_message_stream(
    request: Request,
    thread_id: str,
    body: MessageRequest,
    user_id: str = Depends(get_current_user_id),
):
    """SSE counterpart to POST /messages — real token-by-token text plus tool_call events as
    they happen. The db session is opened here (not via `async with`) and closed inside the
    generator's `finally`, because it must stay alive for the whole streamed response, which is
    read by Starlette only after this function returns — prepare_message_send's checks run
    synchronously first so a bad request still gets a normal HTTP error, not a broken stream."""
    db_factory = request.app.state.db_factory
    redis = request.app.state.redis
    db = db_factory()
    try:
        thread, state, history = await service.prepare_message_send(
            db, db_factory, redis, user_id, thread_id, body.content
        )
    except Exception:
        await db.close()
        raise

    async def event_stream():
        try:
            async for event in service.stream_agent_reply_for_message(
                db,
                db_factory,
                redis,
                thread,
                state,
                history,
                body.content,
                user_id,
                thread_id,
            ):
                yield _sse(event)
        finally:
            await db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/threads/{thread_id}/tool-approvals/resolve/stream")
async def resolve_tool_approval_stream(
    request: Request,
    thread_id: str,
    body: ToolApprovalResolveRequest,
    user_id: str = Depends(get_current_user_id),
):
    """SSE counterpart to POST /tool-approvals/resolve — same streaming rationale as
    post_message_stream above."""
    db_factory = request.app.state.db_factory
    redis = request.app.state.redis
    db = db_factory()
    try:
        prepared = await service.prepare_tool_approval_resolution(
            db,
            db_factory,
            redis,
            user_id,
            thread_id,
            body.decision,
            body.tool_call_id,
            body.rejection_message,
        )
    except Exception:
        await db.close()
        raise

    if isinstance(prepared, service.PostMessageOutcome):
        await db.close()
        return JSONResponse(
            {"status": prepared.kind, "detail": prepared.detail}, status_code=410
        )

    thread, state, pending = prepared

    async def event_stream():
        try:
            async for event in service.stream_agent_reply_for_resume(
                db,
                db_factory,
                redis,
                thread,
                state,
                pending,
                body.decision,
                body.tool_call_id,
                body.rejection_message,
                user_id,
                thread_id,
            ):
                yield _sse(event)
        finally:
            await db.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/threads/{thread_id}/stop")
async def stop_chat_stream(
    request: Request, thread_id: str, user_id: str = Depends(get_current_user_id)
):
    """Cancels a turn currently streaming for this thread — a real mid-generation interrupt
    (agents SDK RunResultStreaming.cancel), not just the client walking away from the fetch.
    Requires the same thread access as reading/posting to it, so one user can't cancel another
    project's in-flight turn just by guessing a thread_id."""
    async with request.app.state.db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        if thread is None or thread.is_deleted:
            raise HTTPException(status_code=404, detail=f"No chat thread {thread_id}")
        await require_thread_access(db, user_id, thread)
    return {"cancelled": cancel_chat_stream(thread_id)}


@router.post("/messages/{message_id}/feedback")
async def set_message_feedback(
    request: Request,
    message_id: int,
    body: FeedbackRequest,
    user_id: str = Depends(get_current_user_id),
):
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    async with request.app.state.db_factory() as db:
        await service.set_message_feedback(db, user_id, message_id, body.rating)
    return {"status": "ok"}


@router.delete("/messages/{message_id}/feedback")
async def delete_message_feedback(
    request: Request, message_id: int, user_id: str = Depends(get_current_user_id)
):
    async with request.app.state.db_factory() as db:
        await service.set_message_feedback(db, user_id, message_id, None)
    return {"status": "ok"}
