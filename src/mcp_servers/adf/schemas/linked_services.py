"""Linked-service tool specs — mirrors claude-desktop/mcp_adf/schemas/linked_services.py's
per-kind layout. 8 generic-dispatch operations plus 1 always-distinct tool (get_linked_service,
name/type only — distinct from get_linked_service_definition_raw's full connection details)."""

from mcp_servers.adf.schemas.base import REASON_PROP, ADFToolSpec, schema

TOOLS = [
    ADFToolSpec(
        name="get_linked_service_definition_raw",
        registry_tool_name="get_resource_definition_raw",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Full linked-service definition — the actual configured host/port/connection string live in "
            "typeProperties (e.g. AzureSqlDatabase's typeProperties.connectionString), which "
            "get_linked_service omits. Use during diagnosis of network/config failures to see the real "
            "configured server address, and as the editable structure for "
            "update_linked_service_definition."
        ),
        params_json_schema=schema(
            {"service_name": {"type": "string"}}, ["service_name"]
        ),
    ),
    ADFToolSpec(
        name="create_linked_service",
        registry_tool_name="create_resource",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Creates a brand-new linked service. Fails with an explicit error if a linked service with this "
            "name already exists — use update_linked_service_definition to modify an existing one instead. "
            "`definition` should be the same flat shape get_linked_service_definition_raw/"
            "update_linked_service_definition use."
        ),
        params_json_schema=schema(
            {
                "service_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Linked service definition JSON.",
                },
                "reason": REASON_PROP,
            },
            ["service_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="list_linked_services",
        registry_tool_name="list_resources",
        resource_type="linked_service",
        name_kwarg=None,
        description=(
            "Factory-wide linked-service sweep, vs. get_linked_service's single lookup. Useful "
            "when the failing linked service isn't known by name up front."
        ),
        params_json_schema=schema({}, []),
    ),
    ADFToolSpec(
        name="update_linked_service_definition",
        registry_tool_name="update_resource_definition",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Overwrites a linked service's full definition (e.g. to correct a wrong host/port). "
            "`definition` must be get_linked_service_definition_raw's output with edits applied."
        ),
        params_json_schema=schema(
            {
                "service_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Modified output of get_linked_service_definition_raw.",
                },
                "reason": REASON_PROP,
            },
            ["service_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="get_linked_service",
        registry_tool_name="get_linked_service",
        resource_type=None,
        name_kwarg=None,
        description=(
            'Name and type only (e.g. "AzureSqlDatabase") — does NOT include the actual configured '
            "host/port/connection string. Use get_linked_service_definition_raw for the real "
            "connection details."
        ),
        params_json_schema=schema(
            {"service_name": {"type": "string"}}, ["service_name"]
        ),
    ),
]
