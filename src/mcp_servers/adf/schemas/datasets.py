"""Dataset tool specs. get_dataset_definition is a cheap read-only summary so a schema-drift
question doesn't require paying for the full wire-format payload every time; the other
operations are generic-dispatch."""

from mcp_servers.adf.schemas.base import REASON_PROP, ADFToolSpec, schema

TOOLS = [
    ADFToolSpec(
        name="get_dataset_definition",
        registry_tool_name="get_dataset_definition",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Fetch a dataset's type, backing linked service, and declared schema/column names — the "
            "part actually needed to diagnose schema_drift (a column renamed/removed/added upstream). "
            "For the full editable structure (parameters, folder, annotations) needed to write a fix, "
            "use get_dataset_definition_raw instead."
        ),
        params_json_schema=schema(
            {"dataset_name": {"type": "string"}}, ["dataset_name"]
        ),
    ),
    ADFToolSpec(
        name="get_dataset_definition_raw",
        registry_tool_name="get_resource_definition_raw",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Full dataset definition (schema, structure, linked service reference, parameters) — the "
            "editable structure required to write a fix via update_dataset_definition. For just checking "
            "column names/types during diagnosis, use get_dataset_definition instead — it's the same "
            "schema info without the rest of the wire-format payload."
        ),
        params_json_schema=schema(
            {"dataset_name": {"type": "string"}}, ["dataset_name"]
        ),
    ),
    ADFToolSpec(
        name="create_dataset",
        registry_tool_name="create_resource",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Creates a brand-new dataset. Fails with an explicit error if a dataset with this name already "
            "exists — use update_dataset_definition to modify an existing one instead. `definition` accepts "
            "either the flat shape get_dataset_definition_raw uses, or the ARM/Data-Factory-Studio export "
            'shape ({"name": ..., "properties": {"type": "...", ...}}) — if a "properties" key is '
            "present, its contents are used and the wrapper is discarded. `dataset_name` (not the JSON's "
            'own "name" field, if present) determines the actual name created.'
        ),
        params_json_schema=schema(
            {
                "dataset_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Dataset definition JSON.",
                },
                "reason": REASON_PROP,
            },
            ["dataset_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="list_datasets",
        registry_tool_name="list_resources",
        resource_type="dataset",
        name_kwarg=None,
        description="Factory-wide dataset sweep — name, type, and backing linked service for each.",
        params_json_schema=schema({}, []),
    ),
    ADFToolSpec(
        name="update_dataset_definition",
        registry_tool_name="update_resource_definition",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Overwrites a dataset's full definition to apply a concrete fix (e.g. correcting a drifted "
            "schema). `definition` must be get_dataset_definition_raw's output with edits applied."
        ),
        params_json_schema=schema(
            {
                "dataset_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Modified output of get_dataset_definition_raw.",
                },
                "reason": REASON_PROP,
            },
            ["dataset_name", "definition", "reason"],
        ),
    ),
]
