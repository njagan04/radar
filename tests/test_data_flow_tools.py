"""Mirrors test_dataset_tools.py's approach for the data_flow resource kind — real Postgres
checkpoints + real SDK (de)serialization, only Azure network calls mocked."""
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DataFlowResource
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotCursor
from mcp_servers.adf.tools import data_flows as dfl

PROJECT = "adf_mcp_test"
KIND = "data_flow"
RESOURCE = "DF_TOOL_PYTEST"

_CREDS = dict(
    factory_name="f", subscription_id="s", resource_group="rg",
    tenant_id="t", client_id="c", client_secret="secret",
)


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


def _data_flow_response(definition: dict, etag: str = 'W/"1"') -> DataFlowResource:
    resource = DataFlowResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


@pytest.mark.asyncio
async def test_create_data_flow_pushes_checkpoints(db):
    definition = {"type": "MappingDataFlow", "typeProperties": {"sources": [], "sinks": [], "transformations": []}}
    mock_client = MagicMock()
    mock_client.data_flows.get.side_effect = ResourceNotFoundError("not found")
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(definition)

    with patch.object(dfl, "_client", return_value=mock_client):
        result = await dfl.create_data_flow(
            db, PROJECT, data_flow_name=RESOURCE, definition=definition, reason="test create", **_CREDS,
        )

    assert result["created"] is True
    assert result["saved_state_name"] == "created"
    snaps = await dfl.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["before-creation", "created"]


@pytest.mark.asyncio
async def test_create_data_flow_unwraps_arm_export_shape(db):
    """create_data_flow accepts either the flat shape or {"name": ..., "properties": {...}} —
    the "properties" contents get used, the wrapper (and its own "name") discarded."""
    flat_definition = {"type": "MappingDataFlow"}
    arm_wrapped = {"name": "ignored-name", "properties": flat_definition}
    mock_client = MagicMock()
    mock_client.data_flows.get.side_effect = ResourceNotFoundError("not found")
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(flat_definition)

    with patch.object(dfl, "_client", return_value=mock_client):
        result = await dfl.create_data_flow(
            db, PROJECT, data_flow_name=RESOURCE, definition=arm_wrapped, reason="test", **_CREDS,
        )

    assert result["created"] is True
    # data_flow_name (not the ARM wrapper's "name") determined what was actually created.
    assert result["data_flow_name"] == RESOURCE


@pytest.mark.asyncio
async def test_create_data_flow_errors_when_already_exists(db):
    mock_client = MagicMock()
    mock_client.data_flows.get.return_value = MagicMock()

    with patch.object(dfl, "_client", return_value=mock_client):
        result = await dfl.create_data_flow(
            db, PROJECT, data_flow_name=RESOURCE, definition={"type": "MappingDataFlow"}, reason="test", **_CREDS,
        )

    assert result == {"error": "data_flow_already_exists", "data_flow_name": RESOURCE}
    mock_client.data_flows.create_or_update.assert_not_called()


@pytest.mark.asyncio
async def test_update_data_flow_definition_ensures_baseline(db):
    existing = {"type": "MappingDataFlow", "script": "old script"}
    new = {"type": "MappingDataFlow", "script": "new script"}

    mock_client = MagicMock()
    mock_client.data_flows.get.return_value = _data_flow_response(existing)
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(new)

    with patch.object(dfl, "_client", return_value=mock_client):
        result = await dfl.update_data_flow_definition(
            db, PROJECT, data_flow_name=RESOURCE, definition=new,
            reason="fix business logic", change_summary="update transformation script", **_CREDS,
        )

    assert result["updated"] is True
    snaps = await dfl.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["initial", "update-transformation-script"]


@pytest.mark.asyncio
async def test_rollback_data_flow_definition_requires_confirmation_then_deletes(db):
    definition = {"type": "MappingDataFlow"}
    await dfl.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await dfl.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="seed", definition=definition)
    await db.commit()

    mock_client = MagicMock()
    with patch.object(dfl, "_client", return_value=mock_client):
        pending = await dfl.rollback_data_flow_definition(
            db, PROJECT, data_flow_name=RESOURCE, reason="undo", state_name="before-creation", **_CREDS,
        )
        assert pending["requires_confirmation"] is True
        mock_client.data_flows.delete.assert_not_called()

        confirmed = await dfl.rollback_data_flow_definition(
            db, PROJECT, data_flow_name=RESOURCE, reason="undo", state_name="before-creation",
            confirm_delete=True, **_CREDS,
        )

    assert confirmed["deleted"] is True
    mock_client.data_flows.delete.assert_called_once()
    snaps = await dfl.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3  # rollback grows history


@pytest.mark.asyncio
async def test_back_then_forward_data_flow_definition(db):
    v1 = {"type": "MappingDataFlow", "marker": "v1"}
    v2 = {"type": "MappingDataFlow", "marker": "v2"}
    await dfl.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await dfl.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v1", action="exists", reason="seed", definition=v1)
    await dfl.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v2", action="exists", reason="seed", definition=v2)
    await db.commit()

    mock_client = MagicMock()
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(v1)

    with patch.object(dfl, "_client", return_value=mock_client):
        back_result = await dfl.back_data_flow_definition(db, PROJECT, data_flow_name=RESOURCE, reason="undo", **_CREDS)
        assert back_result["state_name"] == "v1"

        forward_result = await dfl.forward_data_flow_definition(db, PROJECT, data_flow_name=RESOURCE, reason="redo", **_CREDS)
        assert forward_result["state_name"] == "v2"

    snaps = await dfl.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3  # back/forward never grow history
