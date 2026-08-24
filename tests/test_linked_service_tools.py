"""Mirrors test_pipeline_tools.py's approach for the linked_service resource kind — real SDK
(de)serialization, only Azure network calls mocked. create_linked_service/
update_linked_service_definition are plain sync functions — the checkpoint system was
removed 2026-08-12."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import LinkedServiceResource

from mcp_servers.adf.tools import linked_services as ls

RESOURCE = "LS_TOOL_PYTEST"

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def _linked_service_response(
    definition: dict, etag: str = 'W/"1"'
) -> LinkedServiceResource:
    resource = LinkedServiceResource.deserialize({"properties": definition})
    resource.etag = etag
    return resource


def test_create_linked_service_creates_and_returns_etag():
    definition = {
        "type": "AzureSqlDatabase",
        "typeProperties": {"connectionString": "server=x"},
    }
    mock_client = MagicMock()
    mock_client.linked_services.get.side_effect = ResourceNotFoundError("not found")
    mock_client.linked_services.create_or_update.return_value = (
        _linked_service_response(definition)
    )

    with patch.object(ls, "_client", return_value=mock_client):
        result = ls.create_linked_service(
            service_name=RESOURCE, definition=definition, reason="test create", **_CREDS
        )

    assert result["created"] is True
    assert result["service_name"] == RESOURCE


def test_create_linked_service_errors_when_already_exists():
    mock_client = MagicMock()
    mock_client.linked_services.get.return_value = MagicMock()

    with patch.object(ls, "_client", return_value=mock_client):
        result = ls.create_linked_service(
            service_name=RESOURCE,
            definition={"type": "AzureSqlDatabase"},
            reason="test",
            **_CREDS,
        )

    assert result == {
        "error": "linked_service_already_exists",
        "service_name": RESOURCE,
    }
    mock_client.linked_services.create_or_update.assert_not_called()


def test_update_linked_service_definition_overwrites_and_returns_etag():
    new = {
        "type": "AzureSqlDatabase",
        "typeProperties": {"connectionString": "server=new"},
    }

    mock_client = MagicMock()
    mock_client.linked_services.create_or_update.return_value = (
        _linked_service_response(new)
    )

    with patch.object(ls, "_client", return_value=mock_client):
        result = ls.update_linked_service_definition(
            service_name=RESOURCE, definition=new, reason="fix host", **_CREDS
        )

    assert result["updated"] is True
    assert result["service_name"] == RESOURCE
