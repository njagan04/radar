"""Data-flow tool specs. get_data_flow_definition is a cheap read-only summary so the model
doesn't need the full transformation script just to see what sources/sinks/transformations
exist; the other operations are generic-dispatch."""

from mcp_servers.adf.schemas.base import REASON_PROP, ADFToolSpec, schema

TOOLS = [
    ADFToolSpec(
        name="get_data_flow_definition",
        registry_tool_name="get_data_flow_definition",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Fetch a data flow's type and the names of its sources/sinks/transformations only, not the "
            "full transformation script. Use this first to see the shape of the data flow; once you know "
            "which named transformation to inspect, use get_data_flow_definition_raw for its actual logic."
        ),
        params_json_schema=schema(
            {"data_flow_name": {"type": "string"}}, ["data_flow_name"]
        ),
    ),
    ADFToolSpec(
        name="get_data_flow_definition_raw",
        registry_tool_name="get_resource_definition_raw",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Full Mapping Data Flow definition (sources, sinks, transformation script). Pipeline "
            "definition tools only show that an activity references a data flow by name — this is the "
            "only way to see (and diagnose schema_drift/business_logic failures inside) the "
            "transformation graph itself. For just the shape (source/sink/transformation names) without "
            "the full script, use get_data_flow_definition instead."
        ),
        params_json_schema=schema(
            {"data_flow_name": {"type": "string"}}, ["data_flow_name"]
        ),
    ),
    ADFToolSpec(
        name="create_data_flow",
        registry_tool_name="create_resource",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Creates a brand-new data flow. Fails with an explicit error if a data flow with this name "
            "already exists — use update_data_flow_definition to modify an existing one instead. "
            "`definition` accepts either the flat shape get_data_flow_definition_raw uses, or the "
            'ARM/Data-Factory-Studio export shape ({"name": ..., "properties": {"type": '
            '"MappingDataFlow", ...}}) — if a "properties" key is present, its contents are used and '
            'the wrapper is discarded. `data_flow_name` (not the JSON\'s own "name" field, if present) '
            "determines the actual name created."
        ),
        params_json_schema=schema(
            {
                "data_flow_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Data flow definition JSON.",
                },
                "reason": REASON_PROP,
            },
            ["data_flow_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="list_data_flows",
        registry_tool_name="list_resources",
        resource_type="data_flow",
        name_kwarg=None,
        description="Factory-wide data flow sweep — name and type (e.g. MappingDataFlow) for each.",
        params_json_schema=schema({}, []),
    ),
    ADFToolSpec(
        name="update_data_flow_definition",
        registry_tool_name="update_resource_definition",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Overwrites a data flow's full definition to apply a concrete fix. `definition` must be "
            "get_data_flow_definition_raw's output with edits applied."
        ),
        params_json_schema=schema(
            {
                "data_flow_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Modified output of get_data_flow_definition_raw.",
                },
                "reason": REASON_PROP,
            },
            ["data_flow_name", "definition", "reason"],
        ),
    ),
]
