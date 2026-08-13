"""
Business logic behind every chat endpoint — router.py is a thin translation layer over this.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat.access import require_admin, require_project_access, require_thread_access
from chat.history import to_input_list
from chat.summarization import maybe_summarize
from config.settings import settings
from db.models import (
    AuditLog,
    ChatAnalytics,
    ChatMessage,
    ChatThread,
    FailureEvent,
    MessageFeedback,
    RBACPermission,
)
from gateway.concurrency import DistributedSemaphore
from llm import agent as chat_agent
from llm.context import WorkflowContext
from llm.investigation_state import build_chat_state

logger = logging.getLogger(__name__)


def _investigation_semaphore(redis: aioredis.Redis) -> DistributedSemaphore:
    """A fresh handle per call, not a shared instance — DistributedSemaphore tracks its own
    acquired member on `self`, so two concurrent callers sharing one instance would stomp on
    each other's release. The actual distributed state lives in Redis (see gateway/
    concurrency.py), so constructing a new lightweight handle per turn is correct and cheap."""
    return DistributedSemaphore(
        redis=redis,
        key="active_investigations",
        max_count=settings.max_concurrent_investigations,
        lease_seconds=settings.concurrency_lease_seconds,
        max_wait_seconds=settings.concurrency_max_wait_seconds,
    )


# Matches gateway/vault.py's credential-cache TTL. A pending tool approval that sits this long
# is auto-denied on next access (checked here, not a background job).
_APPROVAL_TTL = timedelta(minutes=90)

# ChatMessage.content is NOT NULL; the Agents SDK's final_output can legitimately come back
# None/empty (e.g. a turn whose last step produced only non-text output) — this is what gets
# persisted instead, rather than raising past a request that already succeeded from the model's
# point of view.
_EMPTY_REPLY_FALLBACK = "(no response text was generated for this turn)"

# $/1M tokens. Only one entry since radar only calls settings.azure_openai_deployment.
# "cached_input" is Azure's discounted rate for input tokens served from its own prompt cache
# (a repeated identical prefix, e.g. the system prompt + tool schemas, is cached automatically
# within a turn's tool-calling loop).
_TOKEN_PRICING = {
    "default": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
}


def _estimate_cost(
    model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0
) -> float:
    pricing = _TOKEN_PRICING.get(model, _TOKEN_PRICING["default"])
    uncached_input_tokens = input_tokens - cached_tokens
    return round(
        (uncached_input_tokens / 1_000_000) * pricing["input"]
        + (cached_tokens / 1_000_000) * pricing["cached_input"]
        + (output_tokens / 1_000_000) * pricing["output"],
        6,
    )


@dataclass
class PostMessageOutcome:
    """Return shape for both post_message and resolve_tool_approval."""

    kind: str  # "message" | "pending_approval" | "expired"
    message: ChatMessage | None = None
    pending_tools: list[dict] | None = None
    detail: str | None = None


async def _get_thread_or_404(db: AsyncSession, thread_id: str) -> ChatThread:
    thread = await db.get(ChatThread, thread_id)
    if thread is None or thread.is_deleted:
        raise HTTPException(status_code=404, detail=f"No chat thread {thread_id}")
    return thread


async def set_message_feedback(
    db: AsyncSession, user_id: str, message_id: int, rating: str | None
) -> None:
    """Thumbs up/down on a specific assistant message — the frontend's MessageActions component
    already renders these buttons and toggles local state; this persists it. `rating=None`
    removes the caller's own feedback row (the frontend's own toggle-off interaction), matching
    the DELETE endpoint; POST always passes a real "up"/"down"."""
    if rating not in ("up", "down", None):
        raise HTTPException(
            status_code=400, detail="rating must be 'up', 'down', or null"
        )

    message = await db.get(ChatMessage, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail=f"No message {message_id}")
    thread = await _get_thread_or_404(db, message.thread_id)
    await require_thread_access(db, user_id, thread)

    existing = await db.execute(
        select(MessageFeedback).where(
            MessageFeedback.message_id == message_id, MessageFeedback.user_id == user_id
        )
    )
    row = existing.scalar_one_or_none()

    if rating is None:
        if row is not None:
            await db.delete(row)
            await db.commit()
        return

    now = datetime.now(UTC)
    if row is not None:
        row.rating = rating
        row.updated_at = now
    else:
        db.add(
            MessageFeedback(
                message_id=message_id, user_id=user_id, rating=rating, created_at=now
            )
        )
    await db.commit()


_NOTIFICATION_LIST_LIMIT = 30


async def list_notifications(
    db: AsyncSession, user_id: str, project: str
) -> list[FailureEvent]:
    """Every notification for this project, newest first — both still-pending and already-
    resolved (the frontend renders resolved ones differently, e.g. a checkmark, rather than
    dropping them). Capped at _NOTIFICATION_LIST_LIMIT; this is a recent-activity list, not a
    paginated archive."""
    await require_project_access(db, user_id, project)
    result = await db.execute(
        select(FailureEvent)
        .where(FailureEvent.project == project, FailureEvent.seed_message.isnot(None))
        .order_by(FailureEvent.created_at.desc())
        .limit(_NOTIFICATION_LIST_LIMIT)
    )
    return list(result.scalars().all())


async def mark_notification_seen(
    db: AsyncSession, user_id: str, investigation_id: str
) -> FailureEvent:
    """Called when a specific notification's row is clicked in the list — not when the list
    itself is merely opened, so unclicked rows stay "new" even after being glanced at.
    Idempotent: a second click is a no-op, doesn't bump seen_at to a later time."""
    failure_event = await db.get(FailureEvent, investigation_id)
    if failure_event is None or failure_event.seed_message is None:
        raise HTTPException(
            status_code=404, detail=f"No notification {investigation_id}"
        )
    await require_project_access(db, user_id, failure_event.project)
    if failure_event.seen_at is None:
        failure_event.seen_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(failure_event)
    return failure_event


async def get_pending_notification(
    db: AsyncSession, user_id: str, investigation_id: str
) -> FailureEvent | None:
    """Looks up one specific notification by investigation_id — used by the draft chat page
    opened via `?notification=<investigation_id>` (e.g. from the email link, which is minted
    once and can be clicked again later) to fetch the exact seed text to show, independent of
    whatever the bell's own "latest" happens to be by the time it's opened. Deliberately
    returned even once already resolved — resolved_thread_id is what tells the caller "this
    became a real thread, redirect there" instead of showing an empty draft with no seed text
    (see chat/router.py's _notification_out and the frontend's own redirect-on-resolved logic).
    Returns None only if it genuinely doesn't exist, or the caller lacks project access."""
    failure_event = await db.get(FailureEvent, investigation_id)
    if failure_event is None or failure_event.seed_message is None:
        return None
    await require_project_access(db, user_id, failure_event.project)
    return failure_event


async def list_threads(
    db: AsyncSession, user_id: str, project: str
) -> list[ChatThread]:
    await require_project_access(db, user_id, project)
    result = await db.execute(
        select(ChatThread)
        .where(ChatThread.project == project, ChatThread.is_deleted.is_(False))
        .order_by(func.coalesce(ChatThread.updated_at, ChatThread.created_at).desc())
    )
    return list(result.scalars().all())


async def create_ad_hoc_thread(
    db: AsyncSession,
    user_id: str,
    project: str,
    investigation_id: str | None = None,
) -> ChatThread:
    """investigation_id is set when this thread is being created FROM a pending notification
    (the user clicked the bell, then sent the suggested message or typed their own on that same
    draft page) — mirrors an ordinary ad-hoc thread's lazy creation exactly, just also stamping
    the originating FailureEvent.resolved_thread_id in the same transaction so the notification
    stops showing as pending. A stale/already-resolved/mismatched-project investigation_id is
    silently ignored rather than erroring — the thread still gets created either way, just
    without the link, since the user's message shouldn't be blocked by a notification that
    someone else already claimed or that no longer exists."""
    await require_project_access(db, user_id, project)

    failure_event = None
    if investigation_id is not None:
        candidate = await db.get(FailureEvent, investigation_id)
        if (
            candidate is not None
            and candidate.project == project
            and candidate.resolved_thread_id is None
        ):
            failure_event = candidate

    thread = ChatThread(
        project=project,
        investigation_id=failure_event.investigation_id if failure_event else None,
        created_at=datetime.now(UTC),
    )
    db.add(thread)
    await db.flush()  # populates thread.thread_id before we stamp it below

    if failure_event is not None:
        failure_event.resolved_thread_id = thread.thread_id
        # Sending a message from a notification necessarily means it was already looked at —
        # resolving without having seen it first isn't a real path, so stamp both together
        # rather than leaving seen_at to a separate, easy-to-skip mark-as-seen call.
        if failure_event.seen_at is None:
            failure_event.seen_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(thread)
    return thread


async def get_thread_with_messages(
    db: AsyncSession,
    user_id: str,
    thread_id: str,
    before_id: int | None,
    limit: int,
) -> tuple[ChatThread, list[ChatMessage], bool]:
    """Cursor-paginated scrollback, newest page first — the initial call (before_id=None)
    returns the most recent `limit` messages, and infinite scroll asks for the page before
    whatever the oldest currently-loaded message's id is. Always returns oldest-first within
    the page (the order a chat transcript actually renders in), even though the query itself
    has to walk backwards (ORDER BY id DESC) to select "the `limit` messages right before this
    cursor" — reversed once in Python, not pushed onto Postgres.

    Third return value (has_more) is True when a full page came back, meaning there may be
    another (older) page beyond it — the frontend uses this to decide whether to keep showing
    a "load more" trigger at the top of the scroll area or stop.
    """
    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)

    query = select(ChatMessage).where(ChatMessage.thread_id == thread_id)
    if before_id is not None:
        query = query.where(ChatMessage.id < before_id)
    query = query.order_by(ChatMessage.id.desc()).limit(limit)

    result = await db.execute(query)
    page = list(result.scalars().all())
    page.reverse()
    return thread, page, len(page) == limit


async def get_thread_stats(db: AsyncSession, user_id: str, thread_id: str) -> dict:
    """Backs the sidebar's per-thread "Info" popup — token usage summed across every
    ChatAnalytics row for this thread (real usage, same source as _persist_turn_result writes,
    not the char-based estimate used for the UI's live token_count display), plus when the
    originating notification actually arrived (FailureEvent.created_at) if this thread came
    from one. Author/created_at aren't computed here — the caller already has those on the
    Thread object it fetched to open this popup in the first place, no need to duplicate."""
    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)

    result = await db.execute(
        select(
            func.coalesce(func.sum(ChatAnalytics.input_tokens), 0),
            func.coalesce(func.sum(ChatAnalytics.output_tokens), 0),
        ).where(ChatAnalytics.thread_id == thread_id)
    )
    input_tokens, output_tokens = result.one()

    notification_created_at = None
    if thread.investigation_id:
        failure_event = await db.get(FailureEvent, thread.investigation_id)
        if failure_event is not None:
            notification_created_at = failure_event.created_at

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(input_tokens) + int(output_tokens),
        "notification_created_at": notification_created_at,
    }


async def get_admin_analytics(db: AsyncSession, user_id: str) -> dict:
    """Cross-project usage/cost analytics for the RADAR admin dashboard — unlike every other
    read in this file, deliberately NOT scoped by require_project_access/UserProjectAssignment:
    an admin sees every project's data, gated only by require_admin. Three independent plain
    aggregate queries, one per report shape, same pattern as get_thread_stats above — no
    generic analytics abstraction."""
    await require_admin(db, user_id)

    tokens_sum = ChatAnalytics.input_tokens + ChatAnalytics.output_tokens

    summary_result = await db.execute(
        select(
            func.coalesce(func.sum(tokens_sum), 0),
            func.coalesce(func.sum(ChatAnalytics.estimated_cost), 0.0),
            func.count(func.distinct(ChatAnalytics.thread_id)),
            func.count(func.distinct(ChatAnalytics.project)),
        )
    )
    total_tokens, total_cost, total_threads, total_projects = summary_result.one()

    by_project_result = await db.execute(
        select(
            ChatAnalytics.project,
            func.coalesce(func.sum(tokens_sum), 0),
            func.coalesce(func.sum(ChatAnalytics.estimated_cost), 0.0),
            func.count(func.distinct(ChatAnalytics.thread_id)),
        )
        .group_by(ChatAnalytics.project)
        .order_by(func.sum(ChatAnalytics.estimated_cost).desc())
    )
    by_project = [
        {
            "project": project,
            "total_tokens": int(tokens),
            "estimated_cost": float(cost),
            "thread_count": int(threads),
        }
        for project, tokens, cost, threads in by_project_result.all()
    ]

    by_user_result = await db.execute(
        select(
            ChatAnalytics.user_id,
            func.coalesce(func.sum(tokens_sum), 0),
            func.coalesce(func.sum(ChatAnalytics.estimated_cost), 0.0),
        )
        .where(ChatAnalytics.user_id.isnot(None))
        .group_by(ChatAnalytics.user_id)
        .order_by(func.sum(ChatAnalytics.estimated_cost).desc())
        .limit(20)
    )
    by_user = [
        {"user_id": user_id, "total_tokens": int(tokens), "estimated_cost": float(cost)}
        for user_id, tokens, cost in by_user_result.all()
    ]

    # Last 14 days, bucketed daily. Bucketed in UTC explicitly (func.timezone converts the
    # timestamptz to a plain UTC timestamp first) — date_trunc on a bare timestamptz column
    # truncates in the connection's session timezone (this DB's is IST), which would silently
    # shift day boundaries by 5:30.
    day = func.date_trunc("day", func.timezone("UTC", ChatAnalytics.created_at))
    by_day_result = await db.execute(
        select(
            day,
            func.coalesce(func.sum(tokens_sum), 0),
            func.coalesce(func.sum(ChatAnalytics.estimated_cost), 0.0),
        )
        .where(ChatAnalytics.created_at >= func.now() - text("interval '14 days'"))
        .group_by(day)
        .order_by(day)
    )
    by_day = [
        {
            "date": date.date().isoformat(),
            "total_tokens": int(tokens),
            "estimated_cost": float(cost),
        }
        for date, tokens, cost in by_day_result.all()
    ]

    feedback_result = await db.execute(
        select(MessageFeedback.rating, func.count())
        .join(ChatMessage, ChatMessage.id == MessageFeedback.message_id)
        .join(ChatThread, ChatThread.thread_id == ChatMessage.thread_id)
        .group_by(MessageFeedback.rating)
    )
    feedback_counts = {rating: int(count) for rating, count in feedback_result.all()}

    return {
        "summary": {
            "total_tokens": int(total_tokens),
            "total_cost": float(total_cost),
            "total_threads": int(total_threads),
            "total_projects": int(total_projects),
        },
        "by_project": by_project,
        "by_user": by_user,
        "by_day": by_day,
        "feedback": {
            "up": feedback_counts.get("up", 0),
            "down": feedback_counts.get("down", 0),
        },
    }


async def claim_thread(db: AsyncSession, user_id: str, thread_id: str) -> ChatThread:
    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)

    result = await db.execute(
        update(ChatThread)
        .where(
            ChatThread.thread_id == thread_id, ChatThread.claimed_by_user_id.is_(None)
        )
        .values(claimed_by_user_id=user_id)
    )
    await db.commit()
    if result.rowcount == 0:
        await db.refresh(thread)
        if thread.claimed_by_user_id != user_id:
            raise HTTPException(
                status_code=409,
                detail=f"Already claimed by another user ({thread.claimed_by_user_id})",
            )
    await db.refresh(thread)
    return thread


def _require_claimant_or_unclaimed(thread: ChatThread, user_id: str) -> None:
    """Write access for rename/delete — same claim-gating post_message/approve/deny already
    enforce (require_thread_access alone is read/project-level only, see its own docstring).
    An unclaimed thread has no owner yet, so any project member may still act on it."""
    if thread.claimed_by_user_id is not None and thread.claimed_by_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Thread claimed by another user ({thread.claimed_by_user_id}), not writable by you",
        )


async def rename_thread(
    db: AsyncSession, user_id: str, thread_id: str, title: str
) -> ChatThread:
    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)
    _require_claimant_or_unclaimed(thread, user_id)
    thread.title = title[:60]
    await db.commit()
    await db.refresh(thread)
    return thread


async def delete_thread(db: AsyncSession, user_id: str, thread_id: str) -> None:
    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)
    _require_claimant_or_unclaimed(thread, user_id)
    thread.is_deleted = True
    await db.commit()


async def _ensure_claimed_by(
    db: AsyncSession, thread: ChatThread, user_id: str
) -> None:
    """Auto-claims an unclaimed thread as `user_id`; 403s if claimed by someone else.
    Matches ChatThread's own docstring: write access is per-thread, one claimant."""
    if thread.claimed_by_user_id is None:
        result = await db.execute(
            update(ChatThread)
            .where(
                ChatThread.thread_id == thread.thread_id,
                ChatThread.claimed_by_user_id.is_(None),
            )
            .values(claimed_by_user_id=user_id)
        )
        await db.commit()
        await db.refresh(thread)
        if result.rowcount == 0 and thread.claimed_by_user_id != user_id:
            raise HTTPException(
                status_code=403,
                detail=f"Already claimed by another user ({thread.claimed_by_user_id})",
            )
    elif thread.claimed_by_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Thread claimed by another user ({thread.claimed_by_user_id}), not writable by you",
        )


async def _persist_turn_result(
    db: AsyncSession,
    thread: ChatThread,
    turn_result: chat_agent.ChatTurnResult,
    triggering_message: str,
    user_id: str | None,
    state: dict,
) -> PostMessageOutcome:
    """Shared by post_message and resolve_tool_approval — either persists a final ChatMessage
    (turn completed) or writes an updated thread.pending_tool_approval (hit an interruption,
    possibly a second one on the resume path). `triggering_message` is the message that started
    this whole turn (not necessarily the most recent one on a resumed-after-approval turn) —
    only used here to name the thread, ChatGPT/Claude-style, once the turn actually completes."""
    if turn_result.kind == "pending_approval":
        thread.pending_tool_approval = {
            **turn_result.run_state_json,
            "pending_tools": turn_result.pending_tools,
            "triggering_message": turn_result.triggering_message,
            # Carried forward into resume_chat_turn(_streamed) so the full trace (including
            # whatever's about to be approved/denied) survives into the final persisted
            # message. input_tokens/output_tokens ride along the same way: the eventual
            # ChatAnalytics row below must reflect the whole turn, not just whichever leg
            # happens to complete it.
            "tool_calls": turn_result.tool_calls,
            "thought_seconds": turn_result.thought_seconds,
            "input_tokens": turn_result.input_tokens,
            "output_tokens": turn_result.output_tokens,
            "cached_tokens": turn_result.cached_tokens,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await db.commit()
        return PostMessageOutcome(
            kind="pending_approval", pending_tools=turn_result.pending_tools
        )

    thread.pending_tool_approval = None
    tool_calls_out: dict | None = None
    if turn_result.tool_calls or turn_result.thought_seconds is not None:
        tool_calls_out = {
            "tools_called": turn_result.tool_calls or [],
            "thought_seconds": turn_result.thought_seconds,
        }
    # ChatMessage.content is NOT NULL, but turn_result.reply_text (sourced from the Agents
    # SDK's own result.final_output) is typed str | None and isn't guaranteed non-empty even on
    # a normal completion — falling back here avoids a NOT NULL constraint violation turning an
    # already-persisted user question into an unhandled 500.
    assistant_message = ChatMessage(
        thread_id=thread.thread_id,
        role="assistant",
        content=turn_result.reply_text or _EMPTY_REPLY_FALLBACK,
        tool_calls=tool_calls_out,
        created_at=datetime.now(UTC),
    )
    db.add(assistant_message)

    if thread.title is None:
        thread.title = await chat_agent.generate_chat_title(
            triggering_message, turn_result.reply_text or ""
        )

    model = settings.azure_openai_model
    db.add(
        ChatAnalytics(
            thread_id=thread.thread_id,
            user_id=user_id,
            project=state["project"],
            platform=state["platform"],
            model=model,
            input_tokens=turn_result.input_tokens,
            output_tokens=turn_result.output_tokens,
            cached_tokens=turn_result.cached_tokens,
            estimated_cost=_estimate_cost(
                model,
                turn_result.input_tokens,
                turn_result.output_tokens,
                turn_result.cached_tokens,
            ),
            created_at=datetime.now(UTC),
        )
    )

    await db.commit()
    await db.refresh(assistant_message)
    return PostMessageOutcome(kind="message", message=assistant_message)


async def _revoked_pending_tools(
    db: AsyncSession, pending_tool_names: set[str]
) -> list[str]:
    """Returns the subset of pending_tool_names whose underlying rbac_permissions row is no
    longer allowed=True (revoked while the approval sat pending) — defense in depth alongside
    RBACGateway._check_permission's own live check. rbac_permissions is keyed by the same tool
    names as pending_tool_names (spec.name)."""
    if not pending_tool_names:
        return []
    result = await db.execute(
        select(RBACPermission).where(RBACPermission.tool_name.in_(pending_tool_names))
    )
    rows = {row.tool_name: row for row in result.scalars().all()}
    revoked = []
    for name in pending_tool_names:
        row = rows.get(name)
        if row is None or not row.allowed:
            revoked.append(name)
    return revoked


async def prepare_message_send(
    db: AsyncSession,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    user_id: str,
    thread_id: str,
    content: str,
) -> tuple[ChatThread, dict, list[dict]]:
    """Shared by post_message and stream_agent_reply_for_message — every check/side-effect
    that must happen (and can still raise a normal HTTPException) before the agent is
    invoked, split out so the streaming path can run this ahead of opening its
    StreamingResponse rather than inside the generator (an exception raised after the SSE
    response has started sending can't turn into a clean HTTP error anymore)."""
    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)
    await _ensure_claimed_by(db, thread, user_id)

    if thread.pending_tool_approval is not None:
        raise HTTPException(
            status_code=409,
            detail="This thread has a pending tool approval — resolve it via "
            "/tool-approvals/resolve before sending a new message",
        )

    user_message = ChatMessage(
        thread_id=thread_id,
        role="user",
        content=content,
        created_at=datetime.now(UTC),
    )
    db.add(user_message)
    # Bumps the thread to the top of list_threads' ordering.
    thread.updated_at = datetime.now(UTC)

    await db.commit()

    state = await build_chat_state(db, thread)

    # Only messages after summarized_through_timestamp are fetched; the stored
    # context_summary is folded in as leading context, bounding history growth instead of
    # resending the thread's entire raw history every turn.
    history_query = select(ChatMessage).where(
        ChatMessage.thread_id == thread_id, ChatMessage.id != user_message.id
    )
    if thread.summarized_through_timestamp is not None:
        history_query = history_query.where(
            ChatMessage.created_at > thread.summarized_through_timestamp
        )
    history_query = history_query.order_by(ChatMessage.id)

    history_result = await db.execute(history_query)
    prior_messages = list(history_result.scalars().all())
    history = to_input_list(prior_messages)
    if thread.context_summary:
        history = [
            {
                "role": "user",
                "content": f"[Summary of earlier conversation]\n{thread.context_summary}",
            }
        ] + history
    return thread, state, history


async def post_message(
    db: AsyncSession,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    user_id: str,
    thread_id: str,
    content: str,
) -> PostMessageOutcome:
    thread, state, history = await prepare_message_send(
        db, db_factory, redis, user_id, thread_id, content
    )
    ctx = WorkflowContext(db_factory=db_factory, redis=redis)
    async with _investigation_semaphore(redis):
        turn_result = await chat_agent.run_chat_turn(
            state, ctx, history, content, user_id=user_id
        )

    outcome = await _persist_turn_result(
        db,
        thread,
        turn_result,
        triggering_message=content,
        user_id=user_id,
        state=state,
    )
    if outcome.kind == "message":
        await maybe_summarize(db, thread)
    return outcome


async def _stream_and_persist(
    db: AsyncSession,
    redis: aioredis.Redis,
    thread: ChatThread,
    agent_stream,
    triggering_message: str,
    user_id: str | None,
    state: dict,
) -> AsyncIterator[dict]:
    """Shared tail of stream_agent_reply_for_message/_resume — forwards token/tool_call events
    as they arrive, then persists the terminal event (identical to _persist_turn_result, since
    the agent stream's terminal ChatTurnResult has the same shape whether the turn finished,
    was interrupted for approval, or was stopped mid-generation) and yields the outcome."""
    turn_result = None
    async with _investigation_semaphore(redis):
        async for event in agent_stream:
            if event["type"] in ("token", "tool_call"):
                yield event
            else:
                turn_result = event["result"]

    outcome = await _persist_turn_result(
        db, thread, turn_result, triggering_message, user_id=user_id, state=state
    )
    if outcome.kind == "message":
        await maybe_summarize(db, thread)
        yield {"type": "message", "message": outcome.message}
    else:
        yield {"type": "pending_approval", "pending_tools": outcome.pending_tools}


async def stream_agent_reply_for_message(
    db: AsyncSession,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    thread: ChatThread,
    state: dict,
    history: list[dict],
    content: str,
    user_id: str,
    thread_id: str,
) -> AsyncIterator[dict]:
    ctx = WorkflowContext(db_factory=db_factory, redis=redis)
    agent_stream = chat_agent.stream_chat_turn(
        state, ctx, history, content, user_id=user_id, thread_id=thread_id
    )
    async for event in _stream_and_persist(
        db,
        redis,
        thread,
        agent_stream,
        triggering_message=content,
        user_id=user_id,
        state=state,
    ):
        yield event


async def prepare_tool_approval_resolution(
    db: AsyncSession,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    user_id: str,
    thread_id: str,
    decision: str,
    tool_call_id: str | None,
    rejection_message: str | None,
) -> tuple[ChatThread, dict, dict] | PostMessageOutcome:
    """Shared by resolve_tool_approval and stream_agent_reply_for_resume — see
    prepare_message_send's docstring for why this must run before any streaming starts. The
    "expired" outcome is a normal (non-exception) early return."""
    if decision not in ("approve", "deny"):
        raise HTTPException(
            status_code=400, detail="decision must be 'approve' or 'deny'"
        )

    thread = await _get_thread_or_404(db, thread_id)
    await require_thread_access(db, user_id, thread)
    if thread.claimed_by_user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail=f"Only the claimant ({thread.claimed_by_user_id}) can resolve a pending tool approval",
        )
    if thread.pending_tool_approval is None:
        raise HTTPException(
            status_code=409, detail="No tool approval is pending on this thread"
        )

    # Atomically claim (and clear) the pending approval before doing anything else. Without
    # this, a concurrent duplicate resolve request (double-click, network retry, second tab)
    # would also see pending_tool_approval as non-None, and both would independently resume +
    # execute the approved tool, e.g. triggering the same pipeline rerun multiple times.
    #
    # Uses a row lock, not UPDATE...RETURNING: RETURNING reflects the value after the update
    # (always NULL here), so it can't distinguish "just cleared this" from "was already NULL".
    locked = await db.execute(
        select(ChatThread.pending_tool_approval)
        .where(ChatThread.thread_id == thread_id)
        .with_for_update()
    )
    pending = locked.scalar_one_or_none()
    if pending is None:
        await db.commit()
        raise HTTPException(
            status_code=409, detail="This tool approval was already resolved"
        )
    await db.execute(
        update(ChatThread)
        .where(ChatThread.thread_id == thread_id)
        .values(pending_tool_approval=None)
    )
    await db.commit()

    pending_tools = pending.get("pending_tools", [])
    created_at = datetime.fromisoformat(pending["created_at"])
    state = await build_chat_state(db, thread)

    if datetime.now(UTC) - created_at > _APPROVAL_TTL:
        db.add(
            AuditLog(
                investigation_id=thread.investigation_id,
                thread_id=thread.thread_id,
                pipeline_name=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(UTC),
                event_type="tool_approval_expired",
                user_id=user_id,
                detail={"pending_tools": pending_tools},
            )
        )
        thread.pending_tool_approval = None
        await db.commit()
        return PostMessageOutcome(
            kind="expired",
            detail="Pending approval expired (90-minute TTL) and was auto-denied.",
        )

    revoked = await _revoked_pending_tools(db, {t["tool_name"] for t in pending_tools})
    if revoked:
        db.add(
            AuditLog(
                investigation_id=thread.investigation_id,
                thread_id=thread.thread_id,
                pipeline_name=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(UTC),
                event_type="tool_approval_denied_revoked",
                user_id=user_id,
                detail={"revoked_tools": revoked},
            )
        )
        thread.pending_tool_approval = None
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail=f"Tool(s) no longer allowed, cannot proceed: {revoked}",
        )

    if decision == "deny":
        # RBACGateway.call() never runs for a rejected tool (the SDK short-circuits before the
        # tool function executes), so this is the only audit record of the denial.
        db.add(
            AuditLog(
                investigation_id=thread.investigation_id,
                thread_id=thread.thread_id,
                pipeline_name=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(UTC),
                event_type="tool_approval_denied",
                user_id=user_id,
                detail={
                    "pending_tools": pending_tools,
                    "rejection_message": rejection_message,
                },
            )
        )
        await db.commit()

    return thread, state, pending


async def resolve_tool_approval(
    db: AsyncSession,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    user_id: str,
    thread_id: str,
    decision: str,
    tool_call_id: str | None,
    rejection_message: str | None,
) -> PostMessageOutcome:
    prepared = await prepare_tool_approval_resolution(
        db,
        db_factory,
        redis,
        user_id,
        thread_id,
        decision,
        tool_call_id,
        rejection_message,
    )
    if isinstance(prepared, PostMessageOutcome):
        return prepared
    thread, state, pending = prepared

    ctx = WorkflowContext(db_factory=db_factory, redis=redis)
    async with _investigation_semaphore(redis):
        turn_result = await chat_agent.resume_chat_turn(
            state,
            ctx,
            pending,
            decision,
            tool_call_id,
            rejection_message,
            user_id=user_id,
        )

    outcome = await _persist_turn_result(
        db,
        thread,
        turn_result,
        triggering_message=pending["triggering_message"],
        user_id=user_id,
        state=state,
    )
    if outcome.kind == "message":
        await maybe_summarize(db, thread)
    return outcome


async def stream_agent_reply_for_resume(
    db: AsyncSession,
    db_factory: async_sessionmaker,
    redis: aioredis.Redis,
    thread: ChatThread,
    state: dict,
    pending: dict,
    decision: str,
    tool_call_id: str | None,
    rejection_message: str | None,
    user_id: str,
    thread_id: str,
) -> AsyncIterator[dict]:
    ctx = WorkflowContext(db_factory=db_factory, redis=redis)
    agent_stream = chat_agent.resume_chat_turn_streamed(
        state,
        ctx,
        pending,
        decision,
        tool_call_id,
        rejection_message,
        user_id=user_id,
        thread_id=thread_id,
    )
    async for event in _stream_and_persist(
        db,
        redis,
        thread,
        agent_stream,
        triggering_message=pending["triggering_message"],
        user_id=user_id,
        state=state,
    ):
        yield event
