"""Tests _dispatch.py's resource_type routing — the actual path an agent uses, since
create_pipeline/update_pipeline_definition/etc. are NOT registered as direct tool names
(see tools/__init__.py's docstring)."""
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import PipelineResource
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotCursor
from mcp_servers.adf.tools import _dispatch as dispatch
from mcp_servers.adf.tools import pipelines as p

PROJECT = "adf_mcp_test"
KIND = "pipeline"
RESOURCE = "PL_DISPATCH_PYTEST"


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


def test_unknown_resource_type_returns_error_not_keyerror():
    result = dispatch.list_resources(resource_type="not_a_real_kind")
    assert result["error"] == "unknown_resource_type"
    assert "pipeline" in result["valid_resource_types"]


@pytest.mark.asyncio
async def test_create_resource_dispatches_to_create_pipeline_with_name_remapped(db):
    definition = {"activities": [{"name": "Wait1", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}
    mock_client = MagicMock()
    mock_client.pipelines.get.side_effect = ResourceNotFoundError("not found")
    created = PipelineResource.deserialize(definition)
    created.etag = 'W/"1"'
    mock_client.pipelines.create_or_update.return_value = created

    with patch.object(p, "_client", return_value=mock_client):
        result = await dispatch.create_resource(
            db, PROJECT, resource_type="pipeline", name=RESOURCE,
            definition=definition, reason="dispatch test",
            factory_name="f", subscription_id="s", resource_group="rg",
            tenant_id="t", client_id="c", client_secret="secret",
        )

    # "name" got remapped to "pipeline_name" internally — create_pipeline never sees "name".
    assert result["created"] is True
    assert result["pipeline_name"] == RESOURCE


@pytest.mark.asyncio
async def test_list_resource_snapshots_dispatches_to_pipeline_kind(db):
    await p.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await db.commit()

    result = await dispatch.list_resource_snapshots(
        db, PROJECT, resource_type="pipeline", name=RESOURCE,
        factory_name="f", subscription_id="s", resource_group="rg",
        tenant_id="t", client_id="c", client_secret="secret",
    )

    assert result["pipeline_name"] == RESOURCE
    assert [s["state_name"] for s in result["states"]] == ["before-creation"]


@pytest.mark.asyncio
async def test_list_resource_snapshots_dispatches_to_linked_service_kind(db):
    """Real DB round-trip through the dispatch layer for a second kind — this is exactly
    the path that would have caught linked_services.py's _KIND="linkedservice" (missing
    underscore) mismatch against the CHECK constraint before it shipped."""
    ls_resource = "LS_DISPATCH_PYTEST"

    async def _cleanup_ls():
        # Cursor row must go first — it FK-references resource_snapshots.
        await db.execute(
            ResourceSnapshotCursor.__table__.delete().where(
                ResourceSnapshotCursor.project == PROJECT,
                ResourceSnapshotCursor.kind == "linked_service",
                ResourceSnapshotCursor.resource_name == ls_resource,
            )
        )
        await db.execute(
            ResourceSnapshot.__table__.delete().where(
                ResourceSnapshot.project == PROJECT,
                ResourceSnapshot.kind == "linked_service",
                ResourceSnapshot.resource_name == ls_resource,
            )
        )
        await db.commit()

    await _cleanup_ls()
    await p.ck._push_snapshot(
        db, PROJECT, "linked_service", ls_resource,
        state_name="before-creation", action="create", reason="seed",
    )
    await db.commit()

    result = await dispatch.list_resource_snapshots(
        db, PROJECT, resource_type="linked_service", name=ls_resource,
        factory_name="f", subscription_id="s", resource_group="rg",
        tenant_id="t", client_id="c", client_secret="secret",
    )

    assert result["service_name"] == ls_resource
    assert [s["state_name"] for s in result["states"]] == ["before-creation"]

    await _cleanup_ls()


@pytest.mark.asyncio
async def test_list_resource_snapshots_dispatches_to_trigger_kind(db):
    trg_resource = "TRG_DISPATCH_PYTEST"

    async def _cleanup_trg():
        await db.execute(
            ResourceSnapshotCursor.__table__.delete().where(
                ResourceSnapshotCursor.project == PROJECT,
                ResourceSnapshotCursor.kind == "trigger",
                ResourceSnapshotCursor.resource_name == trg_resource,
            )
        )
        await db.execute(
            ResourceSnapshot.__table__.delete().where(
                ResourceSnapshot.project == PROJECT,
                ResourceSnapshot.kind == "trigger",
                ResourceSnapshot.resource_name == trg_resource,
            )
        )
        await db.commit()

    await _cleanup_trg()
    await p.ck._push_snapshot(
        db, PROJECT, "trigger", trg_resource,
        state_name="before-creation", action="create", reason="seed",
    )
    await db.commit()

    result = await dispatch.list_resource_snapshots(
        db, PROJECT, resource_type="trigger", name=trg_resource,
        factory_name="f", subscription_id="s", resource_group="rg",
        tenant_id="t", client_id="c", client_secret="secret",
    )

    assert result["trigger_name"] == trg_resource
    assert [s["state_name"] for s in result["states"]] == ["before-creation"]

    await _cleanup_trg()


def test_get_resource_definition_raw_deliberately_excludes_trigger():
    """trigger has no get_*_definition_raw equivalent (get_trigger reports only runtime
    state) — confirmed intentional in _dispatch.py's docstring, not an oversight."""
    result = dispatch.get_resource_definition_raw(resource_type="trigger", name="anything")
    assert result["error"] == "unknown_resource_type"
    assert "trigger" not in result["valid_resource_types"]
