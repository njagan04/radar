"""Tests pipeline tools' create/update against the real Azure SDK (de)serialization — only
the network-calling client methods (get/create_or_update) are mocked. create_pipeline/
update_pipeline_definition are plain sync functions — the git-like checkpoint/rollback/back/
forward system that used to make them async (via Postgres access) was removed 2026-08-12."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import PipelineResource

from mcp_servers.adf.tools import pipelines as p

RESOURCE = "PL_TOOL_PYTEST"

_CREDS = {
    "factory_name": "f",
    "subscription_id": "s",
    "resource_group": "rg",
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "secret",
}


def _pipeline_resource_response(
    definition: dict, etag: str = 'W/"1"'
) -> PipelineResource:
    resource = PipelineResource.deserialize(definition)
    resource.etag = etag
    return resource


def test_create_pipeline_creates_and_returns_etag():
    definition = {
        "activities": [
            {
                "name": "Wait1",
                "type": "Wait",
                "typeProperties": {"waitTimeInSeconds": 1},
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.pipelines.get.side_effect = ResourceNotFoundError("not found")
    mock_client.pipelines.create_or_update.return_value = _pipeline_resource_response(
        definition
    )

    with patch.object(p, "_client", return_value=mock_client):
        result = p.create_pipeline(
            pipeline_name=RESOURCE,
            definition=definition,
            reason="test create",
            **_CREDS,
        )

    assert result["created"] is True
    assert result["pipeline_name"] == RESOURCE
    assert result["etag"] == 'W/"1"'
    mock_client.pipelines.create_or_update.assert_called_once()


def test_create_pipeline_errors_when_already_exists():
    mock_client = MagicMock()
    mock_client.pipelines.get.return_value = MagicMock()  # exists, doesn't raise

    with patch.object(p, "_client", return_value=mock_client):
        result = p.create_pipeline(
            pipeline_name=RESOURCE,
            definition={"activities": []},
            reason="test",
            **_CREDS,
        )

    assert result == {"error": "pipeline_already_exists", "pipeline_name": RESOURCE}
    mock_client.pipelines.create_or_update.assert_not_called()


def test_create_pipeline_rejects_miscased_fields():
    # depends_on (snake_case) instead of dependsOn — the classic .as_dict()-fed-back bug.
    bad_definition = {
        "activities": [
            {"name": "A", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}},
            {
                "name": "B",
                "type": "Wait",
                "typeProperties": {"waitTimeInSeconds": 1},
                "depends_on": [
                    {"activity": "A", "dependencyConditions": ["Succeeded"]}
                ],
            },
        ]
    }
    mock_client = MagicMock()
    mock_client.pipelines.get.side_effect = ResourceNotFoundError("not found")

    with patch.object(p, "_client", return_value=mock_client):
        result = p.create_pipeline(
            pipeline_name=RESOURCE, definition=bad_definition, reason="test", **_CREDS
        )

    assert result["error"] == "possible_miscased_fields"
    mock_client.pipelines.create_or_update.assert_not_called()


def test_update_pipeline_definition_overwrites_and_returns_etag():
    new_definition = {
        "activities": [
            {"name": "New", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 2}}
        ]
    }

    mock_client = MagicMock()
    mock_client.pipelines.create_or_update.return_value = _pipeline_resource_response(
        new_definition
    )

    with patch.object(p, "_client", return_value=mock_client):
        result = p.update_pipeline_definition(
            pipeline_name=RESOURCE,
            definition=new_definition,
            reason="test update",
            **_CREDS,
        )

    assert result["updated"] is True
    assert result["pipeline_name"] == RESOURCE
    assert result["etag"] == 'W/"1"'
