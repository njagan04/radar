"""Linked-service tool specs — mirrors claude-desktop/mcp_adf/schemas/linked_services.py's
per-kind layout. 8 generic-dispatch operations plus 1 always-distinct tool (get_linked_service,
name/type only — distinct from get_linked_service_definition_raw's full connection details)."""
from mcp_servers.adf.schemas.base import ADFToolSpec, CONFIRM_DELETE_PROP, REASON_PROP, STATE_NAME_PROP, schema

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
        params_json_schema=schema({"service_name": {"type": "string"}}, ["service_name"]),
    ),
    ADFToolSpec(
        name="create_linked_service",
        registry_tool_name="create_resource",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Creates a brand-new linked service. Fails with an explicit error if a linked service with this "
            "name already exists — use update_linked_service_definition to modify an existing one instead. "
            "Pushes a \"did not exist\" marker onto this linked service's history stack, so "
            "rollback_linked_service_definition can undo the creation (delete it) later. `definition` "
            "should be the same flat shape get_linked_service_definition_raw/"
            "update_linked_service_definition use."
        ),
        params_json_schema=schema({
            "service_name": {"type": "string"},
            "definition": {"type": "object", "description": "Linked service definition JSON."},
            "reason": REASON_PROP,
            "state_name": STATE_NAME_PROP,
        }, ["service_name", "definition", "reason"]),
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
            "`definition` must be get_linked_service_definition_raw's output with edits applied. The "
            "pre-change definition is pushed onto this linked service's named history stack automatically, "
            "so rollback_linked_service_definition can jump back to it later by name (see "
            "list_linked_service_snapshots)."
        ),
        params_json_schema=schema({
            "service_name": {"type": "string"},
            "definition": {"type": "object", "description": "Modified output of get_linked_service_definition_raw."},
            "reason": REASON_PROP,
            "change_summary": {"type": "string", "description": "One-line human-readable diff of what changed."},
            "state_name": STATE_NAME_PROP,
        }, ["service_name", "definition", "reason", "change_summary"]),
    ),
    ADFToolSpec(
        name="list_linked_service_snapshots",
        registry_tool_name="list_resource_snapshots",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Lists every named state saved in this linked service's history (state_name, timestamp, "
            "reason, change_summary), oldest first. Query this to see what's available before calling "
            "rollback_linked_service_definition with a specific state_name."
        ),
        params_json_schema=schema({"service_name": {"type": "string"}}, ["service_name"]),
    ),
    ADFToolSpec(
        name="rollback_linked_service_definition",
        registry_tool_name="rollback_resource_definition",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Jumps directly to a specific named state from list_linked_service_snapshots, wherever it sits "
            "in history — nothing is ever deleted, so you can move back and then forward again by name. "
            "For simple one-step undo/redo without needing a state_name, use "
            "back_linked_service_definition / forward_linked_service_definition instead. Use this (not "
            "back_linked_service_definition) for any general \"roll back\"/\"revert\"/\"undo the damage\" "
            "request, even if the message also says \"previous version\" — reserve "
            "back_linked_service_definition specifically for \"one step back\"/\"go back one version\" "
            "wording. If the target state predates the linked service's creation, this returns "
            "requires_confirmation instead of deleting — ask the human, then re-call with "
            "confirm_delete=true."
        ),
        params_json_schema=schema({
            "service_name": {"type": "string"},
            "reason": REASON_PROP,
            "state_name": {"type": "string", "description": "Name of the state to jump to, from list_linked_service_snapshots."},
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["service_name", "reason", "state_name"]),
    ),
    ADFToolSpec(
        name="back_linked_service_definition",
        registry_tool_name="back_resource_definition",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Steps the linked service's definition back exactly ONE checkpoint through its history, "
            "like `git checkout HEAD~1` — the history log itself is untouched, only which checkpoint the "
            "live linked service currently matches moves. Use for \"go back one version\"/\"step back\""
            "/\"undo the last change\" wording specifically — for reverting to an arbitrary named "
            "checkpoint or a general \"roll back\"/\"revert\" request, use rollback_linked_service_definition "
            "instead. Call forward_linked_service_definition to step forward again; repeated back/forward "
            "calls walk the log correctly either direction. If the step lands before the linked service "
            "existed, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "service_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["service_name", "reason"]),
    ),
    ADFToolSpec(
        name="forward_linked_service_definition",
        registry_tool_name="forward_resource_definition",
        resource_type="linked_service",
        name_kwarg="service_name",
        description=(
            "Steps the linked service's definition forward exactly ONE checkpoint through its "
            "history — the mirror of back_linked_service_definition. Only available after a previous back "
            "step; returns no_later_state_available if the cursor is already at the newest checkpoint."
        ),
        params_json_schema=schema({
            "service_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["service_name", "reason"]),
    ),
    ADFToolSpec(
        name="get_linked_service",
        registry_tool_name="get_linked_service",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Name and type only (e.g. \"AzureSqlDatabase\") — does NOT include the actual configured "
            "host/port/connection string. Use get_linked_service_definition_raw for the real "
            "connection details."
        ),
        params_json_schema=schema({"service_name": {"type": "string"}}, ["service_name"]),
    ),
]
