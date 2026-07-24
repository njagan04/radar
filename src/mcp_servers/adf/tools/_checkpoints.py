"""
Reimplementation of claude-desktop/mcp_adf/tools/_checkpoints.py's git-like checkpoint/
rollback system, on Postgres (resource_snapshots/resource_snapshot_blobs/
resource_snapshot_cursor) instead of local disk. Same design, three differences:

  1. Scoped by `project` in addition to (kind, resource_name) — the disk version was
     single-factory, this one is multi-project from the start.
  2. Async, not sync — Postgres access in this codebase is exclusively async (asyncpg), so
     every function here is a coroutine taking an AsyncSession. Callers that also need to
     make a (synchronous) Azure SDK call must wrap it via run_in_executor themselves; this
     module has no opinion on how delete_fn/apply_fn talk to Azure, only when to call which.

IMPORTANT — rollback and back/forward are deliberately DIFFERENT operations, not one
consolidated engine (an earlier version of this module's docstring claimed otherwise —
wrong, corrected 2026-07-24): back/forward (_navigate) only ever moves the cursor to an
EXISTING sequence, no new row — that's why they're idempotentHint=False (repeated calls
keep moving further). rollback (_apply_rollback) always pushes a BRAND-NEW checkpoint row
recording "rolled back to X, because R, at time T" even though the resulting live content
matches an existing state — that's why it's idempotentHint=True (repeated calls always
produce the same resulting live state, even though the history log itself grows each time).
Matches claude-desktop's original design exactly, where rollback_*_definition never called
the shared _navigate helper either — each per-kind module has its own inline rollback logic
that pushes a fresh entry. _apply_rollback here is that same logic, just shared once.

Storage model (unchanged from the disk version, just relational instead of file-based):
  - resource_snapshot_blobs: content-addressed, keyed by hash(definition) — identical
    content revisited across history (A->B->A) is never duplicated.
  - resource_snapshots: append-only, one row per checkpoint ever taken, ordered by
    `sequence` (per project+kind+resource_name). `action='create'` marks "the resource did
    not exist yet at this point" (blob_hash stays NULL — this is what the pre-delete
    requires_confirmation gate checks for, not a real content snapshot). `action='exists'`
    is a real, content-bearing snapshot.
  - resource_snapshot_cursor: tracks which snapshot the *live* Azure resource currently
    matches, independent of the index tip — enables git-checkout-style back/forward
    without mutating history.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ResourceSnapshot, ResourceSnapshotBlob, ResourceSnapshotCursor


def _hash_definition(definition: dict) -> str:
    """Content hash, first 16 hex chars of sha256(sorted-key JSON) — identical to the disk
    version's _hash_definition, so identical content always dedupes to the same blob."""
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:16]


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "state"


async def _write_blob(db: AsyncSession, definition: dict) -> str:
    blob_hash = _hash_definition(definition)
    stmt = (
        pg_insert(ResourceSnapshotBlob)
        .values(hash=blob_hash, definition=definition)
        .on_conflict_do_nothing(index_elements=["hash"])
    )
    await db.execute(stmt)
    return blob_hash


async def _get_definition(db: AsyncSession, blob_hash: str | None) -> dict | None:
    if blob_hash is None:
        return None
    result = await db.execute(select(ResourceSnapshotBlob.definition).where(ResourceSnapshotBlob.hash == blob_hash))
    return result.scalar_one_or_none()


async def _read_cursor(db: AsyncSession, project: str, kind: str, resource_name: str) -> int | None:
    result = await db.execute(
        select(ResourceSnapshotCursor.current_sequence).where(
            ResourceSnapshotCursor.project == project,
            ResourceSnapshotCursor.kind == kind,
            ResourceSnapshotCursor.resource_name == resource_name,
        )
    )
    return result.scalar_one_or_none()


async def _write_cursor(db: AsyncSession, project: str, kind: str, resource_name: str, sequence: int) -> None:
    stmt = (
        pg_insert(ResourceSnapshotCursor)
        .values(project=project, kind=kind, resource_name=resource_name, current_sequence=sequence)
        .on_conflict_do_update(
            index_elements=["project", "kind", "resource_name"],
            set_={"current_sequence": sequence},
        )
    )
    await db.execute(stmt)


async def _next_sequence(db: AsyncSession, project: str, kind: str, resource_name: str) -> int:
    result = await db.execute(
        select(func.max(ResourceSnapshot.sequence)).where(
            ResourceSnapshot.project == project,
            ResourceSnapshot.kind == kind,
            ResourceSnapshot.resource_name == resource_name,
        )
    )
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def _push_snapshot(
    db: AsyncSession,
    project: str,
    kind: str,
    resource_name: str,
    action: str,
    reason: str,
    state_name: str | None = None,
    definition: dict | None = None,
    change_summary: str | None = None,
) -> dict:
    """
    Appends one line to this resource's history — a timestamped pointer at the content's
    hash, like a git commit pointing at a tree. `state_name` defaults to a slug of
    change_summary (or reason, if no change_summary) when omitted — matches the disk
    version exactly, since callers like update_*_definition often don't have a natural
    name to give the resulting state upfront.

    A genuine push (create/update/rollback) always becomes the new "current" state — the
    cursor advances to this new sequence regardless of where it was left by a previous
    back_*/forward_* call. Only back_*/forward_* (_navigate) move the cursor WITHOUT a push.
    """
    if action not in ("exists", "create"):
        raise ValueError(f"invalid action {action!r} — must be 'exists' or 'create'")
    if action == "create" and definition is not None:
        raise ValueError("action='create' snapshots mark non-existence and must not carry a definition")
    if action == "exists" and definition is None:
        raise ValueError("action='exists' snapshots require a definition")

    resolved_state_name = state_name or _slugify(change_summary or reason)
    blob_hash = await _write_blob(db, definition) if definition is not None else None
    sequence = await _next_sequence(db, project, kind, resource_name)
    db.add(ResourceSnapshot(
        project=project,
        kind=kind,
        resource_name=resource_name,
        sequence=sequence,
        state_name=resolved_state_name,
        action=action,
        reason=reason,
        change_summary=change_summary,
        blob_hash=blob_hash,
        created_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    await _write_cursor(db, project, kind, resource_name, sequence)
    return {"sequence": sequence, "state_name": resolved_state_name, "action": action}


async def _ensure_baseline(
    db: AsyncSession, project: str, kind: str, resource_name: str, current_definition: dict, reason: str,
) -> None:
    """If this resource has no snapshot history yet, capture its current (as-found) state as
    the first checkpoint before any mutation — so an update's history has a true starting
    point, not just "what it became after the first edit." No-op if history already exists."""
    result = await db.execute(
        select(ResourceSnapshot.id)
        .where(
            ResourceSnapshot.project == project,
            ResourceSnapshot.kind == kind,
            ResourceSnapshot.resource_name == resource_name,
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        return
    await _push_snapshot(
        db, project, kind, resource_name,
        state_name="initial", action="exists", reason=reason, definition=current_definition,
    )


async def _find_snapshot(
    db: AsyncSession, project: str, kind: str, resource_name: str, state_name: str,
) -> ResourceSnapshot | None:
    """Newest-first match by state_name — for rollback's arbitrary named jump."""
    result = await db.execute(
        select(ResourceSnapshot)
        .where(
            ResourceSnapshot.project == project,
            ResourceSnapshot.kind == kind,
            ResourceSnapshot.resource_name == resource_name,
            ResourceSnapshot.state_name == state_name,
        )
        .order_by(ResourceSnapshot.sequence.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _step_snapshot(
    db: AsyncSession, project: str, kind: str, resource_name: str, direction: str,
) -> ResourceSnapshot | dict:
    """
    Moves the cursor's *conceptual* position by one step ("back" or "forward") in the
    ordered sequence of this resource's checkpoints. Does not itself persist the cursor —
    the caller (_navigate) only does that after a successful mutation. Returns the target
    ResourceSnapshot row, or an {"error": ...} dict at either end of history / if none exists.
    """
    result = await db.execute(
        select(ResourceSnapshot.sequence)
        .where(
            ResourceSnapshot.project == project,
            ResourceSnapshot.kind == kind,
            ResourceSnapshot.resource_name == resource_name,
        )
        .order_by(ResourceSnapshot.sequence)
    )
    sequences = [row[0] for row in result.all()]
    if not sequences:
        return {"error": "no_history"}

    current_sequence = await _read_cursor(db, project, kind, resource_name)
    if current_sequence is None or current_sequence not in sequences:
        # No cursor yet (or a stale one) — anchor to the newest, assuming the live resource
        # matches the latest known snapshot.
        current_sequence = sequences[-1]

    idx = sequences.index(current_sequence)
    target_idx = idx - 1 if direction == "back" else idx + 1
    if target_idx < 0:
        return {"error": "no_earlier_state_available"}
    if target_idx >= len(sequences):
        return {"error": "no_later_state_available"}

    result = await db.execute(
        select(ResourceSnapshot).where(
            ResourceSnapshot.project == project,
            ResourceSnapshot.kind == kind,
            ResourceSnapshot.resource_name == resource_name,
            ResourceSnapshot.sequence == sequences[target_idx],
        )
    )
    return result.scalar_one()


async def _navigate(
    db: AsyncSession,
    project: str,
    kind: str,
    resource_name: str,
    target: ResourceSnapshot,
    delete_fn: Callable[[], Awaitable[None]],
    apply_fn: Callable[[dict], Awaitable[None]],
    confirm_delete: bool,
) -> dict:
    """
    Shared back_*/forward_* engine ONLY — not rollback (see _apply_rollback below for why
    they're deliberately different). Given an already-resolved target (via _step_snapshot),
    moves the cursor to it WITHOUT pushing a new history row — a pure relative step, like
    `git checkout HEAD~1`. This is why back/forward are idempotentHint=False: repeated calls
    keep moving further, nothing anchors them to a fixed result.

    requires_confirmation gate: if the target's action is 'create' (meaning "the resource
    did not exist yet at this point in history") and confirm_delete isn't set, returns a
    pending-confirmation dict WITHOUT calling delete_fn — the caller must re-invoke with
    confirm_delete=True after human sign-off. The cursor is only written after the live
    mutation actually succeeds, so a returned error/confirmation-pending state never
    silently advances it.

    delete_fn/apply_fn are async callables the per-kind wrapper supplies — this function has
    no opinion on how they talk to Azure (typically wrapping a still-synchronous SDK call via
    run_in_executor internally), only on whether to call which one and when.
    """
    if target.action == "create" and not confirm_delete:
        return {
            "requires_confirmation": True,
            "would": "delete",
            "target_state": target.state_name,
            "message": (
                f"Stepping to state '{target.state_name}' would DELETE this resource "
                "(it did not exist yet at that point in history). Re-invoke with "
                "confirm_delete=True after human sign-off to proceed."
            ),
        }

    if target.action == "create":
        await delete_fn()
    else:
        definition = await _get_definition(db, target.blob_hash)
        await apply_fn(definition)

    await _write_cursor(db, project, kind, resource_name, target.sequence)
    return {"sequence": target.sequence, "state_name": target.state_name, "action": target.action}


async def _apply_rollback(
    db: AsyncSession,
    project: str,
    kind: str,
    resource_name: str,
    target: ResourceSnapshot,
    reason: str,
    delete_fn: Callable[[], Awaitable[None]],
    apply_fn: Callable[[dict], Awaitable[None]],
    confirm_delete: bool,
) -> dict:
    """
    Rollback engine — given an already-resolved target (via _find_snapshot, an arbitrary
    named jump, not a relative step). Deliberately NOT the same as _navigate: rolling back
    always pushes a BRAND-NEW checkpoint row recording "rolled back to X, because R, at time
    T" via _push_snapshot, even though the resulting live content matches an existing state
    — the history log grows, unlike back/forward which only move the cursor. This is why
    rollback_*_definition is idempotentHint=True: repeated calls always produce the same
    resulting live state, regardless of how many new log rows they add along the way.

    Same requires_confirmation gate and delete_fn/apply_fn contract as _navigate.
    """
    if target.action == "create" and not confirm_delete:
        return {
            "requires_confirmation": True,
            "would": "delete",
            "target_state": target.state_name,
            "message": (
                f"Rolling back to state '{target.state_name}' would DELETE this resource "
                "(it did not exist yet at that point in history). Re-invoke with "
                "confirm_delete=True after human sign-off to proceed."
            ),
        }

    change_summary = f"rolled back to '{target.state_name}'"
    if target.action == "create":
        await delete_fn()
        saved = await _push_snapshot(
            db, project, kind, resource_name,
            state_name=target.state_name, action="create", reason=reason, change_summary=change_summary,
        )
        return {"rolled_back_to": saved["state_name"], "deleted": True, "reason": reason}

    definition = await _get_definition(db, target.blob_hash)
    await apply_fn(definition)
    saved = await _push_snapshot(
        db, project, kind, resource_name,
        state_name=target.state_name, action="exists", reason=reason, change_summary=change_summary,
        definition=definition,
    )
    return {"rolled_back_to": saved["state_name"], "reason": reason}


async def list_snapshots(db: AsyncSession, project: str, kind: str, resource_name: str) -> list[dict]:
    result = await db.execute(
        select(ResourceSnapshot)
        .where(
            ResourceSnapshot.project == project,
            ResourceSnapshot.kind == kind,
            ResourceSnapshot.resource_name == resource_name,
        )
        .order_by(ResourceSnapshot.sequence)
    )
    return [
        {
            "sequence": row.sequence,
            "state_name": row.state_name,
            "action": row.action,
            "reason": row.reason,
            "change_summary": row.change_summary,
            "created_at": row.created_at.isoformat(),
        }
        for row in result.scalars().all()
    ]
