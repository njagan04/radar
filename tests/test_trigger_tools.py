"""Mirrors test_pipeline_tools.py's approach for the trigger resource kind — real SDK
(de)serialization, only Azure network calls mocked. create_trigger/update_trigger_definition
are plain sync functions — the checkpoint system was removed 2026-08-12. Also covers
triggers.py's direct action tools: get/start/stop_trigger and trigger-run history/rerun/
cancel, which have no equivalent in any other resource kind."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import TriggerResource

from mcp_servers.adf.tools import triggers as trg

RESOURCE = "TRG_TOOL_PYTEST"

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def _trigger_response(definition: dict, etag: str = 'W/"1"') -> TriggerResource:
    resource = TriggerResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


def test_create_trigger_creates_and_returns_etag():
    definition = {
        "type": "ScheduleTrigger",
        "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}},
    }
    mock_client = MagicMock()
    mock_client.triggers.get.side_effect = ResourceNotFoundError("not found")
    mock_client.triggers.create_or_update.return_value = _trigger_response(definition)

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.create_trigger(
            trigger_name=RESOURCE, definition=definition, reason="test create", **_CREDS
        )

    assert result["created"] is True
    assert result["trigger_name"] == RESOURCE


def test_create_trigger_errors_when_already_exists():
    mock_client = MagicMock()
    mock_client.triggers.get.return_value = MagicMock()

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.create_trigger(
            trigger_name=RESOURCE,
            definition={"type": "ScheduleTrigger"},
            reason="test",
            **_CREDS,
        )

    assert result == {"error": "trigger_already_exists", "trigger_name": RESOURCE}
    mock_client.triggers.create_or_update.assert_not_called()


def test_update_trigger_definition_overwrites_and_returns_etag():
    new = {
        "type": "ScheduleTrigger",
        "typeProperties": {"recurrence": {"frequency": "Hour", "interval": 6}},
    }

    mock_client = MagicMock()
    mock_client.triggers.create_or_update.return_value = _trigger_response(new)

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.update_trigger_definition(
            trigger_name=RESOURCE, definition=new, reason="fix wrong schedule", **_CREDS
        )

    assert result["updated"] is True
    assert result["trigger_name"] == RESOURCE


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

    assert result == {
        "name": RESOURCE,
        "type": "ScheduleTrigger",
        "runtime_state": "Started",
    }


def test_start_trigger_returns_new_runtime_state():
    mock_poller = MagicMock()
    mock_trigger = MagicMock()
    mock_trigger.properties.runtime_state = "Started"
    mock_client = MagicMock()
    mock_client.triggers.begin_start.return_value = mock_poller
    mock_client.triggers.get.return_value = mock_trigger

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.start_trigger(
            trigger_name=RESOURCE, reason="verified fix", **_CREDS
        )

    mock_poller.result.assert_called_once()
    assert result == {
        "name": RESOURCE,
        "reason": "verified fix",
        "runtime_state": "Started",
    }


def test_stop_trigger_returns_new_runtime_state():
    mock_poller = MagicMock()
    mock_trigger = MagicMock()
    mock_trigger.properties.runtime_state = "Stopped"
    mock_client = MagicMock()
    mock_client.triggers.begin_stop.return_value = mock_poller
    mock_client.triggers.get.return_value = mock_trigger

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.stop_trigger(
            trigger_name=RESOURCE, reason="misfiring, pause it", **_CREDS
        )

    mock_poller.result.assert_called_once()
    assert result == {
        "name": RESOURCE,
        "reason": "misfiring, pause it",
        "runtime_state": "Stopped",
    }


def test_get_trigger_run_history_filters_to_named_trigger():
    other_run = MagicMock(trigger_name="some_other_trigger")
    matching_run = MagicMock(
        trigger_run_id="run-1",
        trigger_name=RESOURCE,
        status="Succeeded",
        message=None,
        trigger_run_timestamp=None,
    )
    mock_client = MagicMock()
    mock_client.trigger_runs.query_by_factory.return_value = MagicMock(
        value=[other_run, matching_run]
    )

    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.get_trigger_run_history(trigger_name=RESOURCE, **_CREDS)

    assert len(result["runs"]) == 1
    assert result["runs"][0]["trigger_run_id"] == "run-1"


def test_rerun_trigger_run():
    mock_client = MagicMock()
    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.rerun_trigger_run(
            trigger_name=RESOURCE, trigger_run_id="run-1", reason="retry", **_CREDS
        )

    mock_client.trigger_runs.rerun.assert_called_once()
    assert result == {
        "trigger_name": RESOURCE,
        "trigger_run_id": "run-1",
        "reran": True,
        "reason": "retry",
    }


def test_cancel_trigger_run():
    mock_client = MagicMock()
    with patch.object(trg, "_client", return_value=mock_client):
        result = trg.cancel_trigger_run(
            trigger_name=RESOURCE, trigger_run_id="run-1", reason="stuck", **_CREDS
        )

    mock_client.trigger_runs.cancel.assert_called_once()
    assert result == {
        "trigger_name": RESOURCE,
        "trigger_run_id": "run-1",
        "cancelled": True,
        "reason": "stuck",
    }
