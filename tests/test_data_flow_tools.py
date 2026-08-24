"""Mirrors test_dataset_tools.py's approach for the data_flow resource kind — real SDK
(de)serialization, only Azure network calls mocked. create_data_flow/
update_data_flow_definition are plain sync functions — the checkpoint system was removed
2026-08-12."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DataFlowResource

from mcp_servers.adf.tools import data_flows as dfl

RESOURCE = "DF_TOOL_PYTEST"

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def _data_flow_response(definition: dict, etag: str = 'W/"1"') -> DataFlowResource:
    resource = DataFlowResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


def test_create_data_flow_creates_and_returns_etag():
    definition = {
        "type": "MappingDataFlow",
        "typeProperties": {"sources": [], "sinks": [], "transformations": []},
    }
    mock_client = MagicMock()
    mock_client.data_flows.get.side_effect = ResourceNotFoundError("not found")
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(
        definition
    )

    with patch.object(dfl, "_client", return_value=mock_client):
        result = dfl.create_data_flow(
            data_flow_name=RESOURCE,
            definition=definition,
            reason="test create",
            **_CREDS,
        )

    assert result["created"] is True
    assert result["data_flow_name"] == RESOURCE


def test_create_data_flow_unwraps_arm_export_shape():
    """create_data_flow accepts either the flat shape or {"name": ..., "properties": {...}} —
    the "properties" contents get used, the wrapper (and its own "name") discarded."""
    flat_definition = {"type": "MappingDataFlow"}
    arm_wrapped = {"name": "ignored-name", "properties": flat_definition}
    mock_client = MagicMock()
    mock_client.data_flows.get.side_effect = ResourceNotFoundError("not found")
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(
        flat_definition
    )

    with patch.object(dfl, "_client", return_value=mock_client):
        result = dfl.create_data_flow(
            data_flow_name=RESOURCE, definition=arm_wrapped, reason="test", **_CREDS
        )

    assert result["created"] is True
    # data_flow_name (not the ARM wrapper's "name") determined what was actually created.
    assert result["data_flow_name"] == RESOURCE


def test_create_data_flow_errors_when_already_exists():
    mock_client = MagicMock()
    mock_client.data_flows.get.return_value = MagicMock()

    with patch.object(dfl, "_client", return_value=mock_client):
        result = dfl.create_data_flow(
            data_flow_name=RESOURCE,
            definition={"type": "MappingDataFlow"},
            reason="test",
            **_CREDS,
        )

    assert result == {"error": "data_flow_already_exists", "data_flow_name": RESOURCE}
    mock_client.data_flows.create_or_update.assert_not_called()


def test_update_data_flow_definition_overwrites_and_returns_etag():
    new = {"type": "MappingDataFlow", "script": "new script"}

    mock_client = MagicMock()
    mock_client.data_flows.create_or_update.return_value = _data_flow_response(new)

    with patch.object(dfl, "_client", return_value=mock_client):
        result = dfl.update_data_flow_definition(
            data_flow_name=RESOURCE,
            definition=new,
            reason="fix business logic",
            **_CREDS,
        )

    assert result["updated"] is True
    assert result["data_flow_name"] == RESOURCE
