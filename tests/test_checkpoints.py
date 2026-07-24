"""
Integration tests against the real dev Postgres DB (not mocked) — the checkpoint/rollback
system's correctness depends on real SQL behavior (upsert-dedup, ordering, sequence
generation) that a fake DB double would only re-assert rather than verify. Uses the already-
seeded 'adf_mcp_test' project; cleans up its own rows before and after each test.
"""
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotBlob, ResourceSnapshotCursor
from mcp_servers.adf.tools import _checkpoints as ck

PROJECT = "adf_mcp_test"
KIND = "pipeline"
RESOURCE = "PL_CHECKPOINT_PYTEST"


@pytest.fixture
async def db():
    engine = create_async_engine(settings.database_url)
    db_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with db_factory() as session:
        await _cleanup(session)
        yield session
        await _cleanup(session)
    await engine.dispose()


async def _cleanup(session):
    await session.execute(
        delete(ResourceSnapshotCursor).where(
            ResourceSnapshotCursor.project == PROJECT,
            ResourceSnapshotCursor.kind == KIND,
            ResourceSnapshotCursor.resource_name == RESOURCE,
        )
    )
    await session.execute(
        delete(ResourceSnapshot).where(
            ResourceSnapshot.project == PROJECT,
            ResourceSnapshot.kind == KIND,
            ResourceSnapshot.resource_name == RESOURCE,
        )
    )
    await session.commit()


async def _seed_create_and_content(db, definition: dict):
    await ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="create")
    await ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="create", definition=definition)
    await db.commit()


@pytest.mark.asyncio
async def test_push_and_list_snapshots(db):
    await _seed_create_and_content(db, {"name": RESOURCE, "activities": []})
    snaps = await ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["before-creation", "created"]
    assert snaps[0]["action"] == "create"
    assert snaps[1]["action"] == "exists"


@pytest.mark.asyncio
async def test_blob_dedup_across_identical_content(db):
    definition = {"name": RESOURCE, "x": 1}
    hash_1 = await ck._write_blob(db, definition)
    hash_2 = await ck._write_blob(db, definition)
    await db.commit()
    assert hash_1 == hash_2
    count = await db.execute(select(func.count()).select_from(ResourceSnapshotBlob).where(ResourceSnapshotBlob.hash == hash_1))
    assert count.scalar_one() == 1


@pytest.mark.asyncio
async def test_step_back_and_forward(db):
    v1 = {"name": RESOURCE, "v": 1}
    v2 = {"name": RESOURCE, "v": 2}
    await _seed_create_and_content(db, v1)
    await ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v2", action="exists", reason="update", definition=v2)
    await db.commit()

    # No cursor yet -> anchors at newest (v2); stepping back lands on "created" (v1).
    target = await ck._step_snapshot(db, PROJECT, KIND, RESOURCE, direction="back")
    assert target.state_name == "created"

    # Stepping back again reaches the create marker.
    await ck._write_cursor(db, PROJECT, KIND, RESOURCE, target.sequence)
    await db.commit()
    target2 = await ck._step_snapshot(db, PROJECT, KIND, RESOURCE, direction="back")
    assert target2.action == "create"

    # One more step back runs off the start of history.
    await ck._write_cursor(db, PROJECT, KIND, RESOURCE, target2.sequence)
    await db.commit()
    result = await ck._step_snapshot(db, PROJECT, KIND, RESOURCE, direction="back")
    assert result == {"error": "no_earlier_state_available"}


@pytest.mark.asyncio
async def test_step_snapshot_no_history_returns_error(db):
    result = await ck._step_snapshot(db, PROJECT, KIND, "PL_DOES_NOT_EXIST", direction="back")
    assert result == {"error": "no_history"}


@pytest.mark.asyncio
async def test_navigate_requires_confirmation_before_delete(db):
    await _seed_create_and_content(db, {"name": RESOURCE})
    create_marker = await ck._find_snapshot(db, PROJECT, KIND, RESOURCE, "before-creation")

    called = {"delete": False, "apply": False}

    async def delete_fn():
        called["delete"] = True

    async def apply_fn(_definition):
        called["apply"] = True

    result = await ck._navigate(db, PROJECT, KIND, RESOURCE, create_marker, delete_fn, apply_fn, confirm_delete=False)

    assert result["requires_confirmation"] is True
    assert result["would"] == "delete"
    assert called == {"delete": False, "apply": False}


@pytest.mark.asyncio
async def test_navigate_deletes_when_confirmed(db):
    await _seed_create_and_content(db, {"name": RESOURCE})
    create_marker = await ck._find_snapshot(db, PROJECT, KIND, RESOURCE, "before-creation")

    called = {"delete": False}

    async def delete_fn():
        called["delete"] = True

    async def apply_fn(_definition):
        pass

    result = await ck._navigate(db, PROJECT, KIND, RESOURCE, create_marker, delete_fn, apply_fn, confirm_delete=True)
    await db.commit()

    assert called["delete"] is True
    assert result["sequence"] == create_marker.sequence
    cursor = await ck._read_cursor(db, PROJECT, KIND, RESOURCE)
    assert cursor == create_marker.sequence


@pytest.mark.asyncio
async def test_navigate_applies_definition_for_exists_snapshot(db):
    definition = {"name": RESOURCE, "marker": "v1-content"}
    await _seed_create_and_content(db, definition)
    created = await ck._find_snapshot(db, PROJECT, KIND, RESOURCE, "created")

    received = {}

    async def delete_fn():
        pass

    async def apply_fn(applied_definition):
        received["definition"] = applied_definition

    await ck._navigate(db, PROJECT, KIND, RESOURCE, created, delete_fn, apply_fn, confirm_delete=False)
    await db.commit()

    assert received["definition"] == definition


@pytest.mark.asyncio
async def test_ensure_baseline_is_noop_when_history_exists(db):
    await _seed_create_and_content(db, {"name": RESOURCE})
    before = len(await ck.list_snapshots(db, PROJECT, KIND, RESOURCE))

    await ck._ensure_baseline(db, PROJECT, KIND, RESOURCE, {"name": "ignored"}, reason="update")
    await db.commit()

    after = len(await ck.list_snapshots(db, PROJECT, KIND, RESOURCE))
    assert before == after


@pytest.mark.asyncio
async def test_ensure_baseline_creates_initial_when_no_history(db):
    definition = {"name": RESOURCE, "as_found": True}
    await ck._ensure_baseline(db, PROJECT, KIND, RESOURCE, definition, reason="update")
    await db.commit()

    snaps = await ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 1
    assert snaps[0]["state_name"] == "initial"


@pytest.mark.asyncio
async def test_push_snapshot_defaults_state_name_to_slug_of_change_summary(db):
    saved = await ck._push_snapshot(
        db, PROJECT, KIND, RESOURCE, action="exists", reason="fix",
        change_summary="Increased Wait Timeout!!", definition={"name": RESOURCE},
    )
    await db.commit()
    assert saved["state_name"] == "increased-wait-timeout"


@pytest.mark.asyncio
async def test_push_snapshot_defaults_state_name_to_slug_of_reason_when_no_change_summary(db):
    saved = await ck._push_snapshot(
        db, PROJECT, KIND, RESOURCE, action="exists", reason="Fixed the Query", definition={"name": RESOURCE},
    )
    await db.commit()
    assert saved["state_name"] == "fixed-the-query"


@pytest.mark.asyncio
async def test_push_snapshot_always_advances_cursor_to_its_own_sequence(db):
    """A genuine push (create/update/rollback) always becomes the new "current" state,
    overriding wherever a prior back_*/forward_* left the cursor — unlike _navigate, which
    only moves the cursor to an EXISTING sequence without pushing."""
    await _seed_create_and_content(db, {"name": RESOURCE, "v": 1})
    # Simulate a prior back-step leaving the cursor at an older sequence.
    await ck._write_cursor(db, PROJECT, KIND, RESOURCE, 1)
    await db.commit()

    saved = await ck._push_snapshot(
        db, PROJECT, KIND, RESOURCE, action="exists", reason="fix", state_name="v2",
        definition={"name": RESOURCE, "v": 2},
    )
    await db.commit()

    cursor = await ck._read_cursor(db, PROJECT, KIND, RESOURCE)
    assert cursor == saved["sequence"]


@pytest.mark.asyncio
async def test_apply_rollback_pushes_a_new_row_unlike_navigate(db):
    """The real difference between rollback and back/forward: rollback ALWAYS grows history
    (a fresh row recording "rolled back to X, because R"), even though the resulting live
    content matches an existing state. back/forward (_navigate) never grows history — pure
    cursor move. This is why rollback is idempotentHint=True and back/forward are False."""
    definition = {"name": RESOURCE, "marker": "v1-content"}
    await _seed_create_and_content(db, definition)
    before_count = len(await ck.list_snapshots(db, PROJECT, KIND, RESOURCE))
    created = await ck._find_snapshot(db, PROJECT, KIND, RESOURCE, "created")

    async def delete_fn():
        pass

    async def apply_fn(_definition):
        pass

    result = await ck._apply_rollback(
        db, PROJECT, KIND, RESOURCE, created, reason="undo", delete_fn=delete_fn, apply_fn=apply_fn, confirm_delete=False,
    )
    await db.commit()

    assert result["rolled_back_to"] == "created"
    after_count = len(await ck.list_snapshots(db, PROJECT, KIND, RESOURCE))
    assert after_count == before_count + 1  # grew — unlike _navigate, which never adds a row

    snaps = await ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert snaps[-1]["state_name"] == "created"
    assert snaps[-1]["change_summary"] == "rolled back to 'created'"
