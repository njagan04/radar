"""
ADFToolSpec — the declarative shape every ADF tool spec (generic-dispatch or always-distinct)
is built as — plus the small JSON-schema-fragment helpers shared by the per-kind schema files
in this package (pipelines.py, datasets.py, linked_services.py, data_flows.py, triggers.py,
global_parameters.py, integration_runtimes.py). Kept separate from schemas/__init__.py so
those per-kind files can import it without a circular import back to __init__.py (which
imports all of THEM to build the final SPECS list).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ADFToolSpec:
    name: str  # distinct, LLM-facing tool name, e.g. "rollback_dataset_definition"
    registry_tool_name: str  # key into TOOL_REGISTRY / rbac_permissions.tool_name
    resource_type: str | None  # kind string for generic-dispatch-derived specs, else None
    name_kwarg: str | None  # e.g. "pipeline_name" - None when the tool takes no resource name
    description: str
    params_json_schema: dict


def schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


REASON_PROP = {"type": "string", "description": "Why this change is being made — shown to the approver."}
STATE_NAME_PROP = {"type": "string", "description": "Optional name for the saved state. Defaults to a slug if omitted."}
CONFIRM_DELETE_PROP = {
    "type": "boolean",
    "description": "Only set true after the human has explicitly agreed to delete the live resource, "
                    "in response to a prior requires_confirmation result.",
}
