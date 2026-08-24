"""Mirrors test_data_flow_tools.py's approach for the global_parameter resource kind — real
SDK (de)serialization, only Azure network calls mocked. create_global_parameter/
update_global_parameter_definition are plain sync functions — the checkpoint system was
removed 2026-08-12.

Unlike every other kind, global parameters don't map 1:1 onto an addressable Azure resource:
ADF has exactly one "default" GlobalParameterResource per factory, and every tool here reads
the whole properties dict, patches one key, and writes the whole dict back. Tests mock
client.global_parameters.get/create_or_update/delete accordingly (not a per-name lookup)."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import GlobalParameterResource

from mcp_servers.adf.tools import global_parameters as gp

RESOURCE = "GP_TOOL_PYTEST"

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def _gp_response(properties: dict, etag: str = 'W/"1"') -> GlobalParameterResource:
    resource = GlobalParameterResource.deserialize({"properties": properties})
    resource.etag = etag
    return resource


def test_create_global_parameter_creates_and_returns_etag():
    definition = {"type": "String", "value": "prod"}
    mock_client = MagicMock()
    mock_client.global_parameters.get.side_effect = ResourceNotFoundError("not found")
    mock_client.global_parameters.create_or_update.return_value = _gp_response(
        {RESOURCE: definition}
    )

    with patch.object(gp, "_client", return_value=mock_client):
        result = gp.create_global_parameter(
            global_parameter_name=RESOURCE,
            definition=definition,
            reason="test create",
            **_CREDS,
        )

    assert result["created"] is True
    assert result["global_parameter_name"] == RESOURCE


def test_create_global_parameter_merges_with_existing_parameters():
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
        result = gp.create_global_parameter(
            global_parameter_name=RESOURCE,
            definition=definition,
            reason="test",
            **_CREDS,
        )

    assert result["created"] is True
    sent_resource = mock_client.global_parameters.create_or_update.call_args[0][3]
    sent_properties = sent_resource.properties
    assert "other_param" in sent_properties
    assert RESOURCE in sent_properties


def test_create_global_parameter_errors_when_already_exists():
    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response(
        {RESOURCE: {"type": "String", "value": "x"}}
    )

    with patch.object(gp, "_client", return_value=mock_client):
        result = gp.create_global_parameter(
            global_parameter_name=RESOURCE,
            definition={"type": "String", "value": "y"},
            reason="test",
            **_CREDS,
        )

    assert result == {
        "error": "global_parameter_already_exists",
        "global_parameter_name": RESOURCE,
    }
    mock_client.global_parameters.create_or_update.assert_not_called()


def test_update_global_parameter_definition_overwrites_and_returns_etag():
    existing = {RESOURCE: {"type": "String", "value": "old value"}}
    new_value = {"type": "String", "value": "new value"}

    mock_client = MagicMock()
    mock_client.global_parameters.get.return_value = _gp_response(existing)
    mock_client.global_parameters.create_or_update.return_value = _gp_response(
        {RESOURCE: new_value}
    )

    with patch.object(gp, "_client", return_value=mock_client):
        result = gp.update_global_parameter_definition(
            global_parameter_name=RESOURCE,
            definition=new_value,
            reason="fix stale value",
            **_CREDS,
        )

    assert result["updated"] is True
    assert result["global_parameter_name"] == RESOURCE


def test_update_global_parameter_definition_errors_when_not_found():
    mock_client = MagicMock()
    mock_client.global_parameters.get.side_effect = ResourceNotFoundError("not found")

    with patch.object(gp, "_client", return_value=mock_client):
        result = gp.update_global_parameter_definition(
            global_parameter_name=RESOURCE,
            definition={"type": "String", "value": "x"},
            reason="test",
            **_CREDS,
        )

    assert result == {
        "error": "global_parameter_not_found",
        "global_parameter_name": RESOURCE,
    }
    mock_client.global_parameters.create_or_update.assert_not_called()
