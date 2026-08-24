"""Tests _dispatch.py's resource_type routing — the actual path an agent uses, since
create_pipeline/update_pipeline_definition/etc. are NOT registered as direct tool names
(see tools/__init__.py's docstring). All dispatch operations are plain sync functions —
the git-like checkpoint system that used to make create/update async was removed 2026-08-12."""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import PipelineResource

from mcp_servers.adf.tools import _dispatch as dispatch, pipelines as p

RESOURCE = "PL_DISPATCH_PYTEST"


def test_unknown_resource_type_returns_error_not_keyerror():
    result = dispatch.list_resources(resource_type="not_a_real_kind")
    assert result["error"] == "unknown_resource_type"
    assert "pipeline" in result["valid_resource_types"]


def test_create_resource_dispatches_to_create_pipeline_with_name_remapped():
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
    created = PipelineResource.deserialize(definition)
    created.etag = 'W/"1"'
    mock_client.pipelines.create_or_update.return_value = created

    with patch.object(p, "_client", return_value=mock_client):
        result = dispatch.create_resource(
            resource_type="pipeline",
            name=RESOURCE,
            definition=definition,
            reason="dispatch test",
            factory_name="f",
            subscription_id="s",
            resource_group="rg",
            tenant_id="t",
            client_id="c",
            client_secret="secret",
        )

    # "name" got remapped to "pipeline_name" internally — create_pipeline never sees "name".
    assert result["created"] is True
    assert result["pipeline_name"] == RESOURCE


def test_get_resource_definition_raw_deliberately_excludes_trigger():
    """trigger has no get_*_definition_raw equivalent (get_trigger reports only runtime
    state) — confirmed intentional in _dispatch.py's docstring, not an oversight."""
    result = dispatch.get_resource_definition_raw(
        resource_type="trigger", name="anything"
    )
    assert result["error"] == "unknown_resource_type"
    assert "trigger" not in result["valid_resource_types"]
