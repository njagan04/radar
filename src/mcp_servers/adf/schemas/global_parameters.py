"""Global-parameter tool specs — mirrors claude-desktop/mcp_adf/schemas/global_parameters.py's
per-kind layout. All 8 are generic-dispatch operations; global parameters have no
always-distinct tools of their own."""

from mcp_servers.adf.schemas.base import REASON_PROP, ADFToolSpec, schema

TOOLS = [
    ADFToolSpec(
        name="get_global_parameter_definition_raw",
        registry_tool_name="get_resource_definition_raw",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            'Full global parameter definition ({"type": ..., "value": ...}). Feed the returned dict '
            "back into update_global_parameter_definition (with edits applied) to apply a fix."
        ),
        params_json_schema=schema(
            {"global_parameter_name": {"type": "string"}}, ["global_parameter_name"]
        ),
    ),
    ADFToolSpec(
        name="create_global_parameter",
        registry_tool_name="create_resource",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Creates a brand-new global parameter. Fails with an explicit error if one with this name "
            "already exists — use update_global_parameter_definition to modify an existing one instead. "
            '`definition` should be {"type": ..., "value": ...}, the same flat shape '
            "get_global_parameter_definition_raw uses."
        ),
        params_json_schema=schema(
            {
                "global_parameter_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": '{"type": ..., "value": ...} global parameter definition.',
                },
                "reason": REASON_PROP,
            },
            ["global_parameter_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="list_global_parameters",
        registry_tool_name="list_resources",
        resource_type="global_parameter",
        name_kwarg=None,
        description=(
            "Factory-wide global parameter sweep — name, type, and value for each. These are the "
            "factory-level parameters referenced by pipelines/datasets/linked services via "
            "@pipeline().globalParameters.<name>."
        ),
        params_json_schema=schema({}, []),
    ),
    ADFToolSpec(
        name="update_global_parameter_definition",
        registry_tool_name="update_resource_definition",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Overwrites a global parameter's type/value (e.g. to fix a stale connection string or a "
            "flipped environment flag baked in as a global). `definition` must be "
            "get_global_parameter_definition_raw's output with edits applied."
        ),
        params_json_schema=schema(
            {
                "global_parameter_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Modified output of get_global_parameter_definition_raw.",
                },
                "reason": REASON_PROP,
            },
            ["global_parameter_name", "definition", "reason"],
        ),
    ),
]
