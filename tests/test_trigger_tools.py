"""Mirrors test_pipeline_tools.py's approach for the trigger resource kind — real Postgres
checkpoints + real SDK (de)serialization, only Azure network calls mocked. Also covers
triggers.py's direct (non-checkpoint) action tools: get/start/stop_trigger and trigger-run
history/rerun/cancel, which have no equivalent in any other resource kind."""
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import TriggerResource
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from db.models import ResourceSnapshot, ResourceSnapshotCursor
from mcp_servers.adf.tools import triggers as trg

PROJECT = "adf_mcp_test"
KIND = "trigger"
RESOURCE = "TRG_TOOL_PYTEST"

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


def _trigger_response(definition: dict, etag: str = 'W/"1"') -> TriggerResource:
    resource = TriggerResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


@pytest.mark.asyncio
async def test_create_trigger_pushes_checkpoints(db):
    definition = {"type": "ScheduleTrigger", "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}}}
    mock_client = MagicMock()
    mock_client.triggers.get.side_effect = ResourceNotFoundError("not found")
    mock_client.triggers.create_or_update.return_value = _trigger_response(definition)

    with patch.object(trg, "_client", return_value=mock_client):
        result = await trg.create_trigger(
            db, PROJECT, trigger_name=RESOURCE, definition=definition, reason="test create", **_CREDS,
        )

    assert result["created"] is True
    assert result["saved_state_name"] == "created"
    snaps = await trg.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["before-creation", "created"]


@pytest.mark.asyncio
async def test_create_trigger_errors_when_already_exists(db):
    mock_client = MagicMock()
    mock_client.triggers.get.return_value = MagicMock()

    with patch.object(trg, "_client", return_value=mock_client):
        result = await trg.create_trigger(
            db, PROJECT, trigger_name=RESOURCE, definition={"type": "ScheduleTrigger"}, reason="test", **_CREDS,
        )

    assert result == {"error": "trigger_already_exists", "trigger_name": RESOURCE}
    mock_client.triggers.create_or_update.assert_not_called()


@pytest.mark.asyncio
async def test_update_trigger_definition_ensures_baseline(db):
    existing = {"type": "ScheduleTrigger", "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}}}
    new = {"type": "ScheduleTrigger", "typeProperties": {"recurrence": {"frequency": "Hour", "interval": 6}}}

    mock_client = MagicMock()
    mock_client.triggers.get.return_value = _trigger_response(existing)
    mock_client.triggers.create_or_update.return_value = _trigger_response(new)

    with patch.object(trg, "_client", return_value=mock_client):
        result = await trg.update_trigger_definition(
            db, PROJECT, trigger_name=RESOURCE, definition=new,
            reason="fix wrong schedule", change_summary="switch to hourly", **_CREDS,
        )

    assert result["updated"] is True
    snaps = await trg.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert [s["state_name"] for s in snaps] == ["initial", "switch-to-hourly"]


@pytest.mark.asyncio
async def test_rollback_trigger_definition_requires_confirmation_then_deletes(db):
    definition = {"type": "ScheduleTrigger"}
    await trg.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await trg.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="created", action="exists", reason="seed", definition=definition)
    await db.commit()

    mock_client = MagicMock()
    with patch.object(trg, "_client", return_value=mock_client):
        pending = await trg.rollback_trigger_definition(
            db, PROJECT, trigger_name=RESOURCE, reason="undo", state_name="before-creation", **_CREDS,
        )
        assert pending["requires_confirmation"] is True
        mock_client.triggers.delete.assert_not_called()

        confirmed = await trg.rollback_trigger_definition(
            db, PROJECT, trigger_name=RESOURCE, reason="undo", state_name="before-creation",
            confirm_delete=True, **_CREDS,
        )

    assert confirmed["deleted"] is True
    mock_client.triggers.delete.assert_called_once()
    snaps = await trg.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3  # rollback grows history


@pytest.mark.asyncio
async def test_back_then_forward_trigger_definition(db):
    v1 = {"type": "ScheduleTrigger", "marker": "v1"}
    v2 = {"type": "ScheduleTrigger", "marker": "v2"}
    await trg.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="before-creation", action="create", reason="seed")
    await trg.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v1", action="exists", reason="seed", definition=v1)
    await trg.ck._push_snapshot(db, PROJECT, KIND, RESOURCE, state_name="v2", action="exists", reason="seed", definition=v2)
    await db.commit()

    mock_client = MagicMock()
    mock_client.triggers.create_or_update.return_value = _trigger_response(v1)

    with patch.object(trg, "_client", return_value=mock_client):
        back_result = await trg.back_trigger_definition(db, PROJECT, trigger_name=RESOURCE, reason="undo", **_CREDS)
        assert back_result["state_name"] == "v1"

        forward_result = await trg.forward_trigger_definition(db, PROJECT, trigger_name=RESOURCE, reason="redo", **_CREDS)
        assert forward_result["state_name"] == "v2"

    snaps = await trg.ck.list_snapshots(db, PROJECT, KIND, RESOURCE)
    assert len(snaps) == 3  # back/forward never grow history


# --- Direct (non-checkpoint) action tools — no Postgres involvement, plain mocked-client
# unit tests, matching how pipelines.py's equivalent direct tools would be tested. ---

def test_get_trigger_reports_runtime_state():
    mock_trigger = MagicMock()
    mock_trigger.properties.type = "ScheduleTrigger"
    mock_trigger.properties.runtime_state = "Started"
    mock_client = MagicMock()
    mock_client.triggers.get.return_value = mock_trigger

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.get_trigger(trigger_name=RESOURCE, **_CREDS)

    assert result == {"name": RESOURCE, "type": "ScheduleTrigger", "runtime_state": "Started"}


def test_start_trigger_returns_new_runtime_state():
    mock_poller = MagicMock()
    mock_trigger = MagicMock()
    mock_trigger.properties.runtime_state = "Started"
    mock_client = MagicMock()
    mock_client.triggers.begin_start.return_value = mock_poller
    mock_client.triggers.get.return_value = mock_trigger

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.start_trigger(trigger_name=RESOURCE, reason="verified fix", **_CREDS)

    mock_poller.result.assert_called_once()
    assert result == {"name": RESOURCE, "reason": "verified fix", "runtime_state": "Started"}


def test_stop_trigger_returns_new_runtime_state():
    mock_poller = MagicMock()
    mock_trigger = MagicMock()
    mock_trigger.properties.runtime_state = "Stopped"
    mock_client = MagicMock()
    mock_client.triggers.begin_stop.return_value = mock_poller
    mock_client.triggers.get.return_value = mock_trigger

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.stop_trigger(trigger_name=RESOURCE, reason="misfiring, pause it", **_CREDS)

    mock_poller.result.assert_called_once()
    assert result == {"name": RESOURCE, "reason": "misfiring, pause it", "runtime_state": "Stopped"}


def test_get_trigger_run_history_filters_to_named_trigger():
    other_run = MagicMock(trigger_name="some_other_trigger")
    matching_run = MagicMock(
        trigger_run_id="run-1", trigger_name=RESOURCE, status="Succeeded", message=None,
        trigger_run_timestamp=None,
    )
    mock_client = MagicMock()
    mock_client.trigger_runs.query_by_factory.return_value = MagicMock(value=[other_run, matching_run])

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.get_trigger_run_history(trigger_name=RESOURCE, **_CREDS)

    assert len(result["runs"]) == 1
    assert result["runs"][0]["trigger_run_id"] == "run-1"


def test_rerun_trigger_run():
    mock_client = MagicMock()
    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.rerun_trigger_run(trigger_name=RESOURCE, trigger_run_id="run-1", reason="retry", **_CREDS)

    mock_client.trigger_runs.rerun.assert_called_once()
    assert result == {"trigger_name": RESOURCE, "trigger_run_id": "run-1", "reran": True, "reason": "retry"}


def test_cancel_trigger_run():
    mock_client = MagicMock()
    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.cancel_trigger_run(trigger_name=RESOURCE, trigger_run_id="run-1", reason="stuck", **_CREDS)

    mock_client.trigger_runs.cancel.assert_called_once()
    assert result == {"trigger_name": RESOURCE, "trigger_run_id": "run-1", "cancelled": True, "reason": "stuck"}
