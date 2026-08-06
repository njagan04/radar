"""Dataset tool specs — mirrors claude-desktop/mcp_adf/schemas/datasets.py's per-kind layout.
7 are generic-dispatch operations; get_dataset_definition is datasets' one always-distinct
tool (2026-08-06, mirrors pipelines' get_pipeline_definition/get_pipeline_definition_raw
split) — a cheap read-only summary so a schema-drift question doesn't require paying for the
full round-trip-safe wire-format payload every time."""
from mcp_servers.adf.schemas.base import ADFToolSpec, CONFIRM_DELETE_PROP, REASON_PROP, STATE_NAME_PROP, schema

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
        params_json_schema=schema({"dataset_name": {"type": "string"}}, ["dataset_name"]),
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
        params_json_schema=schema({"dataset_name": {"type": "string"}}, ["dataset_name"]),
    ),
    ADFToolSpec(
        name="create_dataset",
        registry_tool_name="create_resource",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Creates a brand-new dataset. Fails with an explicit error if a dataset with this name already "
            "exists — use update_dataset_definition to modify an existing one instead. Pushes a \"did not "
            "exist\" marker onto this dataset's history stack, so rollback_dataset_definition can undo the "
            "creation (delete it) later. `definition` accepts either the flat shape "
            "get_dataset_definition_raw uses, or the ARM/Data-Factory-Studio export shape ({\"name\": ..., "
            "\"properties\": {\"type\": \"...\", ...}}) — if a \"properties\" key is present, its contents "
            "are used and the wrapper is discarded. `dataset_name` (not the JSON's own \"name\" field, if "
            "present) determines the actual name created."
        ),
        params_json_schema=schema({
            "dataset_name": {"type": "string"},
            "definition": {"type": "object", "description": "Dataset definition JSON."},
            "reason": REASON_PROP,
            "state_name": STATE_NAME_PROP,
        }, ["dataset_name", "definition", "reason"]),
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
            "schema). `definition` must be get_dataset_definition_raw's output with edits applied. The "
            "pre-change definition is pushed onto this dataset's named history stack automatically, so "
            "rollback_dataset_definition can jump back to it later by name (see list_dataset_snapshots)."
        ),
        params_json_schema=schema({
            "dataset_name": {"type": "string"},
            "definition": {"type": "object", "description": "Modified output of get_dataset_definition_raw."},
            "reason": REASON_PROP,
            "change_summary": {"type": "string", "description": "One-line human-readable diff of what changed."},
            "state_name": STATE_NAME_PROP,
        }, ["dataset_name", "definition", "reason", "change_summary"]),
    ),
    ADFToolSpec(
        name="list_dataset_snapshots",
        registry_tool_name="list_resource_snapshots",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Lists every named state saved in this dataset's history (state_name, timestamp, "
            "reason, change_summary), oldest first. Query this to see what's available before calling "
            "rollback_dataset_definition with a specific state_name."
        ),
        params_json_schema=schema({"dataset_name": {"type": "string"}}, ["dataset_name"]),
    ),
    ADFToolSpec(
        name="rollback_dataset_definition",
        registry_tool_name="rollback_resource_definition",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Jumps directly to a specific named state from list_dataset_snapshots, wherever it sits in "
            "history — nothing is ever deleted, so you can move back and then forward again by name. For "
            "simple one-step undo/redo without needing a state_name, use back_dataset_definition / "
            "forward_dataset_definition instead. Use this (not back_dataset_definition) for any general "
            "\"roll back\"/\"revert\"/\"undo the damage\" request, even if the message also says "
            "\"previous version\" — reserve back_dataset_definition specifically for \"one step back\"/"
            "\"go back one version\" wording. If the target state predates the dataset's "
            "creation, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "dataset_name": {"type": "string"},
            "reason": REASON_PROP,
            "state_name": {"type": "string", "description": "Name of the state to jump to, from list_dataset_snapshots."},
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["dataset_name", "reason", "state_name"]),
    ),
    ADFToolSpec(
        name="back_dataset_definition",
        registry_tool_name="back_resource_definition",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Steps the dataset's definition back exactly ONE checkpoint through its history, "
            "like `git checkout HEAD~1` — the history log itself is untouched, only which checkpoint the "
            "live dataset currently matches moves. Use for \"go back one version\"/\"step back\""
            "/\"undo the last change\" wording specifically — for reverting to an arbitrary named "
            "checkpoint or a general \"roll back\"/\"revert\" request, use rollback_dataset_definition "
            "instead. Call forward_dataset_definition to step forward again; repeated back/forward calls "
            "walk the log correctly either direction. If the step lands before the dataset "
            "existed, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "dataset_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["dataset_name", "reason"]),
    ),
    ADFToolSpec(
        name="forward_dataset_definition",
        registry_tool_name="forward_resource_definition",
        resource_type="dataset",
        name_kwarg="dataset_name",
        description=(
            "Steps the dataset's definition forward exactly ONE checkpoint through its "
            "history — the mirror of back_dataset_definition. Only available after a previous back step; "
            "returns no_later_state_available if the cursor is already at the newest checkpoint."
        ),
        params_json_schema=schema({
            "dataset_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["dataset_name", "reason"]),
    ),
]
