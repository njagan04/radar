"""integration_runtimes.py has no checkpoint/dispatch involvement at all - plain unit tests
against a mocked Azure client, same shape as triggers.py's direct action-tool tests."""

from unittest.mock import MagicMock, patch

from mcp_servers.adf.tools import integration_runtimes as ir

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def test_get_integration_runtime_status_reports_type_and_state():
    mock_status = MagicMock()
    mock_status.properties.type = "SelfHosted"
    mock_status.properties.state = "Online"
    mock_client = MagicMock()
    mock_client.integration_runtimes.get_status.return_value = mock_status

    with patch.object(ir, "_client", return_value=mock_client):
        result = ir.get_integration_runtime_status(
            integration_runtime_name="IR1", **_CREDS
        )

    assert result == {"name": "IR1", "type": "SelfHosted", "state": "Online"}


def test_start_integration_runtime_returns_new_state():
    mock_poller = MagicMock()
    mock_result = MagicMock()
    mock_result.properties.state = "Started"
    mock_poller.result.return_value = mock_result
    mock_client = MagicMock()
    mock_client.integration_runtimes.begin_start.return_value = mock_poller

    with patch.object(ir, "_client", return_value=mock_client):
        result = ir.start_integration_runtime(
            integration_runtime_name="IR1", reason="verified fix", **_CREDS
        )

    mock_poller.result.assert_called_once()
    assert result == {"name": "IR1", "reason": "verified fix", "state": "Started"}
