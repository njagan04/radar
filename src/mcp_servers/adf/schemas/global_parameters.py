"""Global-parameter tool specs — mirrors claude-desktop/mcp_adf/schemas/global_parameters.py's
per-kind layout. All 8 are generic-dispatch operations; global parameters have no
always-distinct tools of their own."""
from mcp_servers.adf.schemas.base import ADFToolSpec, CONFIRM_DELETE_PROP, REASON_PROP, STATE_NAME_PROP, schema

TOOLS = [
    ADFToolSpec(
        name="get_global_parameter_definition_raw",
        registry_tool_name="get_resource_definition_raw",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Full global parameter definition ({\"type\": ..., \"value\": ...}). Feed the returned dict "
            "back into update_global_parameter_definition (with edits applied) to apply a fix."
        ),
        params_json_schema=schema({"global_parameter_name": {"type": "string"}}, ["global_parameter_name"]),
    ),
    ADFToolSpec(
        name="create_global_parameter",
        registry_tool_name="create_resource",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Creates a brand-new global parameter. Fails with an explicit error if one with this name "
            "already exists — use update_global_parameter_definition to modify an existing one instead. "
            "Pushes a \"did not exist\" marker onto this parameter's history stack, so "
            "rollback_global_parameter_definition can undo the creation (delete it) later. `definition` "
            "should be {\"type\": ..., \"value\": ...}, the same flat shape "
            "get_global_parameter_definition_raw uses."
        ),
        params_json_schema=schema({
            "global_parameter_name": {"type": "string"},
            "definition": {"type": "object", "description": "{\"type\": ..., \"value\": ...} global parameter definition."},
            "reason": REASON_PROP,
            "state_name": STATE_NAME_PROP,
        }, ["global_parameter_name", "definition", "reason"]),
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
            "flipped environment flag baked in as a global). If this parameter has no history yet, its "
            "as-found content is captured as an 'initial' checkpoint first. `definition` must be "
            "get_global_parameter_definition_raw's output with edits applied."
        ),
        params_json_schema=schema({
            "global_parameter_name": {"type": "string"},
            "definition": {"type": "object", "description": "Modified output of get_global_parameter_definition_raw."},
            "reason": REASON_PROP,
            "change_summary": {"type": "string", "description": "One-line human-readable diff of what changed."},
            "state_name": STATE_NAME_PROP,
        }, ["global_parameter_name", "definition", "reason", "change_summary"]),
    ),
    ADFToolSpec(
        name="list_global_parameter_snapshots",
        registry_tool_name="list_resource_snapshots",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Lists every named state saved in this global parameter's history (state_name, timestamp, "
            "reason, change_summary), oldest first. Query this to see what's available before calling "
            "rollback_global_parameter_definition with a specific state_name."
        ),
        params_json_schema=schema({"global_parameter_name": {"type": "string"}}, ["global_parameter_name"]),
    ),
    ADFToolSpec(
        name="rollback_global_parameter_definition",
        registry_tool_name="rollback_resource_definition",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Jumps directly to a specific named state from list_global_parameter_snapshots, wherever it "
            "sits in history — nothing is ever deleted, so you can move back and then forward again by "
            "name. For simple one-step undo/redo without needing a state_name, use "
            "back_global_parameter_definition / forward_global_parameter_definition instead. Use this "
            "(not back_global_parameter_definition) for any general \"roll back\"/\"revert\"/\"undo the "
            "damage\" request, even if the message also says \"previous version\" — reserve "
            "back_global_parameter_definition specifically for \"one step back\"/\"go back one version\" "
            "wording. If the target state predates the global parameter's creation, this returns "
            "requires_confirmation instead of deleting — ask the human, then re-call with "
            "confirm_delete=true."
        ),
        params_json_schema=schema({
            "global_parameter_name": {"type": "string"},
            "reason": REASON_PROP,
            "state_name": {"type": "string", "description": "Name of the state to jump to, from list_global_parameter_snapshots."},
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["global_parameter_name", "reason", "state_name"]),
    ),
    ADFToolSpec(
        name="back_global_parameter_definition",
        registry_tool_name="back_resource_definition",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Steps the global parameter's definition back exactly ONE checkpoint through its history, "
            "like `git checkout HEAD~1` — the history log itself is untouched, only which checkpoint the "
            "live global parameter currently matches moves. Use for \"go back one version\"/\"step back\""
            "/\"undo the last change\" wording specifically — for reverting to an arbitrary named "
            "checkpoint or a general \"roll back\"/\"revert\" request, use "
            "rollback_global_parameter_definition instead. Call forward_global_parameter_definition to "
            "step forward again; repeated back/forward calls walk the log correctly either direction. If "
            "the step lands before the global parameter existed, this returns requires_confirmation "
            "instead of deleting — ask the human, then re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "global_parameter_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["global_parameter_name", "reason"]),
    ),
    ADFToolSpec(
        name="forward_global_parameter_definition",
        registry_tool_name="forward_resource_definition",
        resource_type="global_parameter",
        name_kwarg="global_parameter_name",
        description=(
            "Steps the global parameter's definition forward exactly ONE checkpoint through its "
            "history — the mirror of back_global_parameter_definition. Only available after a previous "
            "back step; returns no_later_state_available if the cursor is already at the newest "
            "checkpoint."
        ),
        params_json_schema=schema({
            "global_parameter_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["global_parameter_name", "reason"]),
    ),
]
