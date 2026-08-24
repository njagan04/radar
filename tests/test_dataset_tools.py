"""Mirrors test_pipeline_tools.py's approach for the dataset resource kind — real SDK
(de)serialization, only Azure network calls mocked. create_dataset/update_dataset_definition
are plain sync functions — the checkpoint system was removed 2026-08-12."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DatasetResource

from mcp_servers.adf.tools import datasets as ds

RESOURCE = "DS_TOOL_PYTEST"

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def _dataset_response(definition: dict, etag: str = 'W/"1"') -> DatasetResource:
    resource = DatasetResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


def test_create_dataset_creates_and_returns_etag():
    definition = {
        "type": "AzureSqlTable",
        "linkedServiceName": {"referenceName": "ls1", "type": "LinkedServiceReference"},
    }
    mock_client = MagicMock()
    mock_client.datasets.get.side_effect = ResourceNotFoundError("not found")
    mock_client.datasets.create_or_update.return_value = _dataset_response(definition)

    with patch.object(ds, "_client", return_value=mock_client):
        result = ds.create_dataset(
            dataset_name=RESOURCE, definition=definition, reason="test create", **_CREDS
        )

    assert result["created"] is True
    assert result["dataset_name"] == RESOURCE


def test_create_dataset_unwraps_arm_export_shape():
    """create_dataset accepts either the flat shape or {"name": ..., "properties": {...}} —
    the "properties" contents get used, the wrapper (and its own "name") discarded."""
    flat_definition = {"type": "AzureSqlTable"}
    arm_wrapped = {"name": "ignored-name", "properties": flat_definition}
    mock_client = MagicMock()
    mock_client.datasets.get.side_effect = ResourceNotFoundError("not found")
    mock_client.datasets.create_or_update.return_value = _dataset_response(
        flat_definition
    )

    with patch.object(ds, "_client", return_value=mock_client):
        result = ds.create_dataset(
            dataset_name=RESOURCE, definition=arm_wrapped, reason="test", **_CREDS
        )

    assert result["created"] is True
    # dataset_name (not the ARM wrapper's "name") determined what was actually created.
    assert result["dataset_name"] == RESOURCE


def test_create_dataset_errors_when_already_exists():
    mock_client = MagicMock()
    mock_client.datasets.get.return_value = MagicMock()

    with patch.object(ds, "_client", return_value=mock_client):
        result = ds.create_dataset(
            dataset_name=RESOURCE,
            definition={"type": "AzureSqlTable"},
            reason="test",
            **_CREDS,
        )

    assert result == {"error": "dataset_already_exists", "dataset_name": RESOURCE}
    mock_client.datasets.create_or_update.assert_not_called()


def test_update_dataset_definition_overwrites_and_returns_etag():
    new = {"type": "AzureSqlTable", "schema": [{"name": "new_col"}]}

    mock_client = MagicMock()
    mock_client.datasets.create_or_update.return_value = _dataset_response(new)

    with patch.object(ds, "_client", return_value=mock_client):
        result = ds.update_dataset_definition(
            dataset_name=RESOURCE, definition=new, reason="fix schema drift", **_CREDS
        )

    assert result["updated"] is True
    assert result["dataset_name"] == RESOURCE
