"""Mirrors test_data_flow_tools.py's approach for the global_parameter resource kind — real
Postgres checkpoints + real SDK (de)serialization, only Azure network calls mocked.

Unlike every other kind, global parameters don't map 1:1 onto an addressable Azure resource:
ADF has exactly one "default" GlobalParameterResource per factory, and every tool here reads
the whole properties dict, patches one key, and writes the whole dict back. Tests mock
client.global_parameters.get/create_or_update/delete accordingly (not a per-name lookup)."""
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import GlobalParameterResource
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotCursor
from mcp_servers.adf.tools import global_parameters as gp

PROJECT = "adf_mcp_test"
KIND = "global_parameter"
RESOURCE = "GP_TOOL_PYTEST"

_CREDS = dict(
    factory_name="f", subscription_id="s", resource_group="rg",
    tenant_id="t", client_id="c", client_secret="secret",
)


@pytest.fixture
async def db():
    engine = create_async_engine(
        settings.database_url,
        connect_args={"server_settings": {"search_path": settings.radar_db_schema}},
    )
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


def _gp_response(properties: dict, etag: str = 'W/"1"') -> GlobalParameterResource:
    resource = GlobalParameterResource.deserialize({"properties": properties})
    resource.etag = etag
    return resource


@pytest.mark.asyncio
async def test_create_global_parameter_pushes_checkpoints(db):
    definition = {"type": "String", "value": "prod"}
    mock_client = MagicMock()
    mock_client.global_parameters.get.side_effect = ResourceNotFoundError("not found")
    mock_client.global_parameters.create_or_update.return_value = _gp_response({RESOURCE: definition})

    with patch.object(gp, "_client", return_value=mock_client):
        result = await gp.create_global_parameter(
            db, PROJECT, global_parameter_name=RESOURCE, definition=definition, reason="test create", **_CREDS,
        )

    assert result["created"] is True
    assert result["saved_state_name"] == "created"
    snaps = await gp.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["before-creation", "created"]


@pytest.mark.asyncio
async def test_create_global_parameter_merges_with_existing_parameters(db):
    """create_global_parameter must read the existing properties dict and ADD to it, not
    overwrite other, unrelated global parameters that already exist in the factory."""
    existing_other = {"other_param": {"type": "String", "value": "unchanged"}}
    definition = {"type": "String", "value": "prod"}
    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response(existing_other)
    mock_client.global_parameters.create_or_update.return_value = _gp_response(
        {**existing_other, RESOURCE: definition}
    )

    with patch.object(gp, "_client", return_value=mock_client):
        result = await gp.create_global_parameter(
            db, PROJECT, global_parameter_name=RESOURCE, definition=definition, reason="test", **_CREDS,
        )

    assert result["created"] is True
    sent_resource = mock_client.global_parameters.create_or_update.call_args[0][3]
    sent_properties = sent_resource.properties
    assert "other_param" in sent_properties
    assert RESOURCE in sent_properties


@pytest.mark.asyncio
async def test_create_global_parameter_errors_when_already_exists(db):
    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response({RESOURCE: {"type": "String", "value": "x"}})

    with patch.object(gp, "_client", return_value=mock_client):
        result = await gp.create_global_parameter(
            db, PROJECT, global_parameter_name=RESOURCE, definition={"type": "String", "value": "y"},
            reason="test", **_CREDS,
        )

    assert result == {"error": "global_parameter_already_exists", "global_parameter_name": RESOURCE}
    mock_client.global_parameters.create_or_update.assert_not_called()


@pytest.mark.asyncio
async def test_update_global_parameter_definition_ensures_baseline(db):
    existing = {RESOURCE: {"type": "String", "value": "old value"}}
    new_value = {"type": "String", "value": "new value"}

    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response(existing)
    mock_client.global_parameters.create_or_update.return_value = _gp_response({RESOURCE: new_value})

    with patch.object(gp, "_client", return_value=mock_client):
        result = await gp.update_global_parameter_definition(
            db, PROJECT, global_parameter_name=RESOURCE, definition=new_value,
            reason="fix stale value", change_summary="point at new value", **_CREDS,
        )

    assert result["updated"] is True
    snaps = await gp.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["initial", "point-at-new-value"]


@pytest.mark.asyncio
async def test_update_global_parameter_definition_errors_when_not_found(db):
    mock_client = MagicMock()
    mock_client.global_parameters.get.side_effect = ResourceNotFoundError("not found")

    with patch.object(gp, "_client", return_value=mock_client):
        result = await gp.update_global_parameter_definition(
            db, PROJECT, global_parameter_name=RESOURCE, definition={"type": "String", "value": "x"},
            reason="test", change_summary="test", **_CREDS,
        )

    assert result == {"error": "global_parameter_not_found", "global_parameter_name": RESOURCE}
    mock_client.global_parameters.create_or_update.assert_not_called()


@pytest.mark.asyncio
async def test_rollback_global_parameter_definition_requires_confirmation_then_deletes(db):
    definition = {"type": "String", "value": "x"}
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="seed", definition=definition)
    await db.commit()

    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response({RESOURCE: definition})

    with patch.object(gp, "_client", return_value=mock_client):
        pending = await gp.rollback_global_parameter_definition(
            db, PROJECT, global_parameter_name=RESOURCE, reason="undo", state_name="before-creation", **_CREDS,
        )
        assert pending["requires_confirmation"] is True
        mock_client.global_parameters.delete.assert_not_called()

        confirmed = await gp.rollback_global_parameter_definition(
            db, PROJECT, global_parameter_name=RESOURCE, reason="undo", state_name="before-creation",
            confirm_delete=True, **_CREDS,
        )

    assert confirmed["deleted"] is True
    # RESOURCE was the only parameter, so deleting it removes the whole "default" resource
    # rather than writing back an empty properties dict.
    mock_client.global_parameters.delete.assert_called_once()
    mock_client.global_parameters.create_or_update.assert_not_called()
    snaps = await gp.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3  # rollback grows history


@pytest.mark.asyncio
async def test_rollback_deletes_only_this_parameter_when_others_remain(db):
    """When other global parameters coexist in the factory, removing this one must write
    back the remaining properties dict, not delete the whole "default" resource."""
    definition = {"type": "String", "value": "x"}
    other = {"other_param": {"type": "String", "value": "unchanged"}}
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="seed", definition=definition)
    await db.commit()

    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response({**other, RESOURCE: definition})
    mock_client.global_parameters.create_or_update.return_value = _gp_response(other)

    with patch.object(gp, "_client", return_value=mock_client):
        result = await gp.rollback_global_parameter_definition(
            db, PROJECT, global_parameter_name=RESOURCE, reason="undo", state_name="before-creation",
            confirm_delete=True, **_CREDS,
        )

    assert result["deleted"] is True
    mock_client.global_parameters.delete.assert_not_called()
    sent_resource = mock_client.global_parameters.create_or_update.call_args[0][3]
    assert RESOURCE not in sent_resource.properties
    assert "other_param" in sent_resource.properties


@pytest.mark.asyncio
async def test_back_then_forward_global_parameter_definition(db):
    v1 = {"type": "String", "value": "v1"}
    v2 = {"type": "String", "value": "v2"}
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v1", action="exists", reason="seed", definition=v1)
    await gp.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v2", action="exists", reason="seed", definition=v2)
    await db.commit()

    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response({RESOURCE: v2})
    mock_client.global_parameters.create_or_update.return_value = _gp_response({RESOURCE: v1})

    with patch.object(gp, "_client", return_value=mock_client):
        back_result = await gp.back_global_parameter_definition(db, PROJECT, global_parameter_name=RESOURCE, reason="undo", **_CREDS)
        assert back_result["state_name"] == "v1"

        forward_result = await gp.forward_global_parameter_definition(db, PROJECT, global_parameter_name=RESOURCE, reason="redo", **_CREDS)
        assert forward_result["state_name"] == "v2"

    snaps = await gp.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3  # back/forward never grow history
