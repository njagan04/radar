"""Mirrors test_pipeline_tools.py's approach for the linked_service resource kind — real
Postgres checkpoints + real SDK (de)serialization, only Azure network calls mocked."""
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import LinkedServiceResource
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotCursor
from mcp_servers.adf.tools import linked_services as ls

PROJECT = "adf_mcp_test"
KIND = "linked_service"
RESOURCE = "LS_TOOL_PYTEST"

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


def _linked_service_response(definition: dict, etag: str = 'W/"1"') -> LinkedServiceResource:
    resource = LinkedServiceResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


@pytest.mark.asyncio
async def test_create_linked_service_pushes_checkpoints(db):
    definition = {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": "server=x"}}
    mock_client = MagicMock()
    mock_client.linked_services.get.side_effect = ResourceNotFoundError("not found")
    mock_client.linked_services.create_or_update.return_value = _linked_service_response(definition)

    with patch.object(ls, "_client", return_value=mock_client):
        result = await ls.create_linked_service(
            db, PROJECT, service_name=RESOURCE, definition=definition, reason="test create", **_CREDS,
        )

    assert result["created"] is True
    assert result["saved_state_name"] == "created"
    snaps = await ls.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["before-creation", "created"]


@pytest.mark.asyncio
async def test_create_linked_service_errors_when_already_exists(db):
    mock_client = MagicMock()
    mock_client.linked_services.get.return_value = MagicMock()

    with patch.object(ls, "_client", return_value=mock_client):
        result = await ls.create_linked_service(
            db, PROJECT, service_name=RESOURCE, definition={"type": "AzureSqlDatabase"}, reason="test", **_CREDS,
        )

    assert result == {"error": "linked_service_already_exists", "service_name": RESOURCE}
    mock_client.linked_services.create_or_update.assert_not_called()


@pytest.mark.asyncio
async def test_update_linked_service_definition_ensures_baseline(db):
    existing = {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": "server=old"}}
    new = {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": "server=new"}}

    mock_client = MagicMock()
    mock_client.linked_services.get.return_value = _linked_service_response(existing)
    mock_client.linked_services.create_or_update.return_value = _linked_service_response(new)

    with patch.object(ls, "_client", return_value=mock_client):
        result = await ls.update_linked_service_definition(
            db, PROJECT, service_name=RESOURCE, definition=new,
            reason="fix host", change_summary="point at new server", **_CREDS,
        )

    assert result["updated"] is True
    snaps = await ls.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["initial", "point-at-new-server"]


@pytest.mark.asyncio
async def test_rollback_linked_service_definition_requires_confirmation_then_deletes(db):
    definition = {"type": "AzureSqlDatabase"}
    await ls.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await ls.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="seed", definition=definition)
    await db.commit()

    mock_client = MagicMock()
    with patch.object(ls, "_client", return_value=mock_client):
        pending = await ls.rollback_linked_service_definition(
            db, PROJECT, service_name=RESOURCE, reason="undo", state_name="before-creation", **_CREDS,
        )
        assert pending["requires_confirmation"] is True
        mock_client.linked_services.delete.assert_not_called()

        confirmed = await ls.rollback_linked_service_definition(
            db, PROJECT, service_name=RESOURCE, reason="undo", state_name="before-creation",
            confirm_delete=True, **_CREDS,
        )

    assert confirmed["deleted"] is True
    mock_client.linked_services.delete.assert_called_once()
    # Rollback grows history (a new row), unlike back/forward.
    snaps = await ls.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3


@pytest.mark.asyncio
async def test_back_then_forward_linked_service_definition(db):
    v1 = {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": "v1"}}
    v2 = {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": "v2"}}
    await ls.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await ls.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v1", action="exists", reason="seed", definition=v1)
    await ls.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v2", action="exists", reason="seed", definition=v2)
    await db.commit()

    mock_client = MagicMock()
    mock_client.linked_services.create_or_update.return_value = _linked_service_response(v1)

    with patch.object(ls, "_client", return_value=mock_client):
        back_result = await ls.back_linked_service_definition(db, PROJECT, service_name=RESOURCE, reason="undo", **_CREDS)
        assert back_result["state_name"] == "v1"

        forward_result = await ls.forward_linked_service_definition(db, PROJECT, service_name=RESOURCE, reason="redo", **_CREDS)
        assert forward_result["state_name"] == "v2"

    # back/forward never grow history — still exactly the 3 seeded rows.
    snaps = await ls.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3
