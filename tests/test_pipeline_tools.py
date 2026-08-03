"""
Tests the new checkpoint-enabled pipeline tools (create/update/rollback/back/forward)
against the REAL Postgres checkpoint system and REAL Azure SDK (de)serialization — only the
actual network-calling client methods (get/create_or_update/delete) are mocked, since
exercising real create/delete cycles against live ADF infrastructure isn't something an
automated test should do repeatedly.
"""
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import PipelineResource
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotCursor
from mcp_servers.adf.tools import pipelines as p

PROJECT = "adf_mcp_test"
KIND = "pipeline"
RESOURCE = "PL_TOOL_PYTEST"

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


def _pipeline_resource_response(definition: dict, etag: str = 'W/"1"') -> PipelineResource:
    resource = PipelineResource.deserialize(definition)
    resource.etag = etag
    return resource


@pytest.mark.asyncio
async def test_create_pipeline_pushes_before_creation_and_created_checkpoints(db):
    definition = {"activities": [{"name": "Wait1", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}
    mock_client = MagicMock()
    mock_client.pipelines.get.side_effect = ResourceNotFoundError("not found")
    mock_client.pipelines.create_or_update.return_value = _pipeline_resource_response(definition)

    with patch.object(p, "_client", return_value=mock_client):
        result = await p.create_pipeline(
            db, PROJECT, pipeline_name=RESOURCE, definition=definition, reason="test create", **_CREDS,
        )

    assert result["created"] is True
    assert result["saved_state_name"] == "created"
    mock_client.pipelines.create_or_update.assert_called_once()

    snaps = await p.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["before-creation", "created"]
    assert snaps[0]["action"] == "create"
    assert snaps[1]["action"] == "exists"


@pytest.mark.asyncio
async def test_create_pipeline_errors_when_already_exists(db):
    mock_client = MagicMock()
    mock_client.pipelines.get.return_value = MagicMock()  # exists, doesn't raise

    with patch.object(p, "_client", return_value=mock_client):
        result = await p.create_pipeline(
            db, PROJECT, pipeline_name=RESOURCE, definition={"activities": []}, reason="test", **_CREDS,
        )

    assert result == {"error": "pipeline_already_exists", "pipeline_name": RESOURCE}
    mock_client.pipelines.create_or_update.assert_not_called()
    snaps = await p.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert snaps == []  # nothing committed on the early-exit path


@pytest.mark.asyncio
async def test_create_pipeline_rejects_miscased_fields_without_writing_anything(db):
    # depends_on (snake_case) instead of dependsOn — the classic .as_dict()-fed-back bug.
    bad_definition = {
        "activities": [
            {"name": "A", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}},
            {
                "name": "B", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1},
                "depends_on": [{"activity": "A", "dependencyConditions": ["Succeeded"]}],
            },
        ]
    }
    mock_client = MagicMock()
    mock_client.pipelines.get.side_effect = ResourceNotFoundError("not found")

    with patch.object(p, "_client", return_value=mock_client):
        result = await p.create_pipeline(
            db, PROJECT, pipeline_name=RESOURCE, definition=bad_definition, reason="test", **_CREDS,
        )

    assert result["error"] == "possible_miscased_fields"
    mock_client.pipelines.create_or_update.assert_not_called()
    # The before-creation snapshot staged before validation was flush()ed (visible to this
    # same open transaction's own reads — expected read-your-own-writes behavior) but never
    # committed. Rolling back the transaction proves nothing durable was written.
    await db.rollback()
    snaps = await p.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert snaps == []


@pytest.mark.asyncio
async def test_update_pipeline_definition_ensures_baseline_then_pushes_new_checkpoint(db):
    existing_definition = {"activities": [{"name": "Old", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}
    new_definition = {"activities": [{"name": "New", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 2}}]}

    mock_client = MagicMock()
    mock_client.pipelines.get.return_value = _pipeline_resource_response(existing_definition)
    mock_client.pipelines.create_or_update.return_value = _pipeline_resource_response(new_definition)

    with patch.object(p, "_client", return_value=mock_client):
        result = await p.update_pipeline_definition(
            db, PROJECT, pipeline_name=RESOURCE, definition=new_definition,
            reason="test update", change_summary="swap activity", **_CREDS,
        )

    assert result["updated"] is True
    snaps = await p.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    # state_name defaults to a SLUG of change_summary when omitted (spaces -> hyphens) —
    # change_summary itself is stored verbatim, unslugified, separately.
    assert [s["state_name"] for s in snaps] == ["initial", "swap-activity"]
    assert snaps[0]["change_summary"] is None
    assert snaps[1]["change_summary"] == "swap activity"


@pytest.mark.asyncio
async def test_rollback_pipeline_definition_requires_confirmation_then_deletes(db):
    definition = {"activities": [{"name": "Wait1", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}
    await p.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await p.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="seed", definition=definition)
    await db.commit()

    mock_client = MagicMock()

    with patch.object(p, "_client", return_value=mock_client):
        pending = await p.rollback_pipeline_definition(
            db, PROJECT, pipeline_name=RESOURCE, reason="undo", state_name="before-creation", **_CREDS,
        )
        assert pending["requires_confirmation"] is True
        mock_client.pipelines.delete.assert_not_called()

        confirmed = await p.rollback_pipeline_definition(
            db, PROJECT, pipeline_name=RESOURCE, reason="undo", state_name="before-creation",
            confirm_delete=True, **_CREDS,
        )

    assert confirmed["deleted"] is True
    assert confirmed["rolled_back_to"] == "before-creation"
    mock_client.pipelines.delete.assert_called_once()


@pytest.mark.asyncio
async def test_rollback_pipeline_definition_unknown_state_returns_error(db):
    mock_client = MagicMock()
    with patch.object(p, "_client", return_value=mock_client):
        result = await p.rollback_pipeline_definition(
            db, PROJECT, pipeline_name=RESOURCE, reason="undo", state_name="does-not-exist", **_CREDS,
        )
    assert result == {"error": "state_not_found", "pipeline_name": RESOURCE, "state_name": "does-not-exist"}


@pytest.mark.asyncio
async def test_back_then_forward_pipeline_definition(db):
    v1 = {"activities": [{"name": "V1", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}
    v2 = {"activities": [{"name": "V2", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 2}}]}
    await p.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await p.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v1", action="exists", reason="seed", definition=v1)
    await p.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v2", action="exists", reason="seed", definition=v2)
    await db.commit()

    mock_client = MagicMock()
    mock_client.pipelines.create_or_update.return_value = _pipeline_resource_response(v1)

    with patch.object(p, "_client", return_value=mock_client):
        back_result = await p.back_pipeline_definition(db, PROJECT, pipeline_name=RESOURCE, reason="undo", **_CREDS)
        assert back_result["state_name"] == "v1"
        mock_client.pipelines.create_or_update.assert_called_once()

        forward_result = await p.forward_pipeline_definition(db, PROJECT, pipeline_name=RESOURCE, reason="redo", **_CREDS)
        assert forward_result["state_name"] == "v2"

        # One more forward runs off the end of history.
        past_end = await p.forward_pipeline_definition(db, PROJECT, pipeline_name=RESOURCE, reason="redo", **_CREDS)
        assert past_end["error"] == "no_later_state_available"
