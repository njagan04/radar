"""Data-flow tool specs — mirrors claude-desktop/mcp_adf/schemas/data_flows.py's per-kind
layout. 7 are generic-dispatch operations; get_data_flow_definition is data flows' one
always-distinct tool (2026-08-06, mirrors pipelines' get_pipeline_definition/
get_pipeline_definition_raw split) — a cheap read-only summary so the model doesn't need the
full transformation script just to see what sources/sinks/transformations exist."""
from mcp_servers.adf.schemas.base import ADFToolSpec, CONFIRM_DELETE_PROP, REASON_PROP, STATE_NAME_PROP, schema

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
        params_json_schema=schema({"data_flow_name": {"type": "string"}}, ["data_flow_name"]),
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
        params_json_schema=schema({"data_flow_name": {"type": "string"}}, ["data_flow_name"]),
    ),
    ADFToolSpec(
        name="create_data_flow",
        registry_tool_name="create_resource",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Creates a brand-new data flow. Fails with an explicit error if a data flow with this name "
            "already exists — use update_data_flow_definition to modify an existing one instead. Pushes a "
            "\"did not exist\" marker onto this data flow's history stack, so "
            "rollback_data_flow_definition can undo the creation (delete it) later. `definition` accepts "
            "either the flat shape get_data_flow_definition_raw uses, or the ARM/Data-Factory-Studio export "
            "shape ({\"name\": ..., \"properties\": {\"type\": \"MappingDataFlow\", ...}}) — if a "
            "\"properties\" key is present, its contents are used and the wrapper is discarded. "
            "`data_flow_name` (not the JSON's own \"name\" field, if present) determines the actual name "
            "created."
        ),
        params_json_schema=schema({
            "data_flow_name": {"type": "string"},
            "definition": {"type": "object", "description": "Data flow definition JSON."},
            "reason": REASON_PROP,
            "state_name": STATE_NAME_PROP,
        }, ["data_flow_name", "definition", "reason"]),
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
            "get_data_flow_definition_raw's output with edits applied. The pre-change definition is pushed "
            "onto this data flow's named history stack automatically, so rollback_data_flow_definition can "
            "jump back to it later by name (see list_data_flow_snapshots)."
        ),
        params_json_schema=schema({
            "data_flow_name": {"type": "string"},
            "definition": {"type": "object", "description": "Modified output of get_data_flow_definition_raw."},
            "reason": REASON_PROP,
            "change_summary": {"type": "string", "description": "One-line human-readable diff of what changed."},
            "state_name": STATE_NAME_PROP,
        }, ["data_flow_name", "definition", "reason", "change_summary"]),
    ),
    ADFToolSpec(
        name="list_data_flow_snapshots",
        registry_tool_name="list_resource_snapshots",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Lists every named state saved in this data flow's history (state_name, timestamp, "
            "reason, change_summary), oldest first. Query this to see what's available before calling "
            "rollback_data_flow_definition with a specific state_name."
        ),
        params_json_schema=schema({"data_flow_name": {"type": "string"}}, ["data_flow_name"]),
    ),
    ADFToolSpec(
        name="rollback_data_flow_definition",
        registry_tool_name="rollback_resource_definition",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Jumps directly to a specific named state from list_data_flow_snapshots, wherever it sits in "
            "history — nothing is ever deleted, so you can move back and then forward again by name. For "
            "simple one-step undo/redo without needing a state_name, use back_data_flow_definition / "
            "forward_data_flow_definition instead. Use this (not back_data_flow_definition) for any general "
            "\"roll back\"/\"revert\"/\"undo the damage\" request, even if the message also says "
            "\"previous version\" — reserve back_data_flow_definition specifically for \"one step back\"/"
            "\"go back one version\" wording. If the target state predates the data flow's "
            "creation, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "data_flow_name": {"type": "string"},
            "reason": REASON_PROP,
            "state_name": {"type": "string", "description": "Name of the state to jump to, from list_data_flow_snapshots."},
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["data_flow_name", "reason", "state_name"]),
    ),
    ADFToolSpec(
        name="back_data_flow_definition",
        registry_tool_name="back_resource_definition",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Steps the data flow's definition back exactly ONE checkpoint through its history, "
            "like `git checkout HEAD~1` — the history log itself is untouched, only which checkpoint the "
            "live data flow currently matches moves. Use for \"go back one version\"/\"step back\""
            "/\"undo the last change\" wording specifically — for reverting to an arbitrary named "
            "checkpoint or a general \"roll back\"/\"revert\" request, use rollback_data_flow_definition "
            "instead. Call forward_data_flow_definition to step forward again; repeated back/forward calls "
            "walk the log correctly either direction. If the step lands before the data flow "
            "existed, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "data_flow_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["data_flow_name", "reason"]),
    ),
    ADFToolSpec(
        name="forward_data_flow_definition",
        registry_tool_name="forward_resource_definition",
        resource_type="data_flow",
        name_kwarg="data_flow_name",
        description=(
            "Steps the data flow's definition forward exactly ONE checkpoint through its "
            "history — the mirror of back_data_flow_definition. Only available after a previous back step; "
            "returns no_later_state_available if the cursor is already at the newest checkpoint."
        ),
        params_json_schema=schema({
            "data_flow_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["data_flow_name", "reason"]),
    ),
]
