"""Trigger tool specs — mirrors claude-desktop/mcp_adf/schemas/triggers.py's per-kind layout.
7 generic-dispatch operations (no get_definition_raw — get_trigger below already reports the
full definition plus runtime state) plus 6 always-distinct trigger-specific tools."""
from mcp_servers.adf.schemas.base import ADFToolSpec, CONFIRM_DELETE_PROP, REASON_PROP, STATE_NAME_PROP, schema

TOOLS = [
    ADFToolSpec(
        name="create_trigger",
        registry_tool_name="create_resource",
        resource_type="trigger",
        name_kwarg="trigger_name",
        description=(
            "Creates a brand-new trigger (e.g. a ScheduleTrigger). Fails with an explicit error if a "
            "trigger with this name already exists — use update_trigger_definition to modify an existing "
            "one instead. Created in a Stopped state, same as ADF Studio's default — call start_trigger "
            "separately once you've verified it. Pushes a \"did not exist\" marker onto this trigger's "
            "history stack, so rollback_trigger_definition can undo the creation (delete it) later."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "definition": {"type": "object", "description": "Trigger definition JSON."},
            "reason": REASON_PROP,
            "state_name": STATE_NAME_PROP,
        }, ["trigger_name", "definition", "reason"]),
    ),
    ADFToolSpec(
        name="list_triggers",
        registry_tool_name="list_resources",
        resource_type="trigger",
        name_kwarg=None,
        description="Factory-wide trigger sweep — every trigger's name, type, and runtime state.",
        params_json_schema=schema({}, []),
    ),
    ADFToolSpec(
        name="update_trigger_definition",
        registry_tool_name="update_resource_definition",
        resource_type="trigger",
        name_kwarg="trigger_name",
        description=(
            "Overwrites a trigger's full definition (e.g. to correct a wrong schedule/recurrence). "
            "`definition` must be get_trigger's raw definition with edits applied. The pre-change "
            "definition is pushed onto this trigger's named history stack automatically, so "
            "rollback_trigger_definition can jump back to it later by name (see list_trigger_snapshots). "
            "Does not change the trigger's Started/Stopped runtime state — use start_trigger/stop_trigger "
            "for that."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "definition": {"type": "object", "description": "Modified output of get_trigger."},
            "reason": REASON_PROP,
            "change_summary": {"type": "string", "description": "One-line human-readable diff of what changed."},
            "state_name": STATE_NAME_PROP,
        }, ["trigger_name", "definition", "reason", "change_summary"]),
    ),
    ADFToolSpec(
        name="list_trigger_snapshots",
        registry_tool_name="list_resource_snapshots",
        resource_type="trigger",
        name_kwarg="trigger_name",
        description=(
            "Lists every named state saved in this trigger's history (state_name, timestamp, "
            "reason, change_summary), oldest first. Query this to see what's available before calling "
            "rollback_trigger_definition with a specific state_name."
        ),
        params_json_schema=schema({"trigger_name": {"type": "string"}}, ["trigger_name"]),
    ),
    ADFToolSpec(
        name="rollback_trigger_definition",
        registry_tool_name="rollback_resource_definition",
        resource_type="trigger",
        name_kwarg="trigger_name",
        description=(
            "Jumps directly to a specific named state from list_trigger_snapshots, wherever it sits in "
            "history — nothing is ever deleted, so you can move back and then forward again by name. For "
            "simple one-step undo/redo without needing a state_name, use back_trigger_definition / "
            "forward_trigger_definition instead. Use this (not back_trigger_definition) for any general "
            "\"roll back\"/\"revert\"/\"undo the damage\" request, even if the message also says "
            "\"previous version\" — reserve back_trigger_definition specifically for \"one step back\"/"
            "\"go back one version\" wording. If the target state predates the trigger's "
            "creation, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "reason": REASON_PROP,
            "state_name": {"type": "string", "description": "Name of the state to jump to, from list_trigger_snapshots."},
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["trigger_name", "reason", "state_name"]),
    ),
    ADFToolSpec(
        name="back_trigger_definition",
        registry_tool_name="back_resource_definition",
        resource_type="trigger",
        name_kwarg="trigger_name",
        description=(
            "Steps the trigger's definition back exactly ONE checkpoint through its history, "
            "like `git checkout HEAD~1` — the history log itself is untouched, only which checkpoint the "
            "live trigger currently matches moves. Use for \"go back one version\"/\"step back\""
            "/\"undo the last change\" wording specifically — for reverting to an arbitrary named "
            "checkpoint or a general \"roll back\"/\"revert\" request, use rollback_trigger_definition "
            "instead. Call forward_trigger_definition to step forward again; repeated back/forward calls "
            "walk the log correctly either direction. If the step lands before the trigger "
            "existed, this returns requires_confirmation instead of deleting — ask the human, then "
            "re-call with confirm_delete=true."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["trigger_name", "reason"]),
    ),
    ADFToolSpec(
        name="forward_trigger_definition",
        registry_tool_name="forward_resource_definition",
        resource_type="trigger",
        name_kwarg="trigger_name",
        description=(
            "Steps the trigger's definition forward exactly ONE checkpoint through its "
            "history — the mirror of back_trigger_definition. Only available after a previous back step; "
            "returns no_later_state_available if the cursor is already at the newest checkpoint."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "reason": REASON_PROP,
            "confirm_delete": CONFIRM_DELETE_PROP,
        }, ["trigger_name", "reason"]),
    ),
    ADFToolSpec(
        name="get_trigger",
        registry_tool_name="get_trigger",
        resource_type=None,
        name_kwarg=None,
        description="Get a trigger's runtime state (Started/Stopped/Disabled) and configuration.",
        params_json_schema=schema({"trigger_name": {"type": "string"}}, ["trigger_name"]),
    ),
    ADFToolSpec(
        name="start_trigger",
        registry_tool_name="start_trigger",
        resource_type=None,
        name_kwarg=None,
        description="Starts a stopped or disabled trigger.",
        params_json_schema=schema({"trigger_name": {"type": "string"}, "reason": REASON_PROP}, ["trigger_name", "reason"]),
    ),
    ADFToolSpec(
        name="stop_trigger",
        registry_tool_name="stop_trigger",
        resource_type=None,
        name_kwarg=None,
        description="Stops a running trigger. Inverse of start_trigger — use to pause a misfiring trigger.",
        params_json_schema=schema({"trigger_name": {"type": "string"}, "reason": REASON_PROP}, ["trigger_name", "reason"]),
    ),
    ADFToolSpec(
        name="get_trigger_run_history",
        registry_tool_name="get_trigger_run_history",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Trigger-run history for a specific trigger — distinct from pipeline-run history. Needed "
            "for tumbling-window and event triggers, where the trigger run itself (not the pipeline "
            "run it invokes) is the unit that can fail, be rerun, or be cancelled."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "days": {"type": "integer", "default": 7},
        }, ["trigger_name"]),
    ),
    ADFToolSpec(
        name="rerun_trigger_run",
        registry_tool_name="rerun_trigger_run",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Reruns a specific trigger run that already failed. Needed for tumbling-window/event "
            "triggers that don't go through rerun_pipeline. Call directly with a placeholder run id "
            "if none was given rather than declining to call."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "trigger_run_id": {"type": "string"},
            "reason": REASON_PROP,
        }, ["trigger_name", "trigger_run_id", "reason"]),
    ),
    ADFToolSpec(
        name="cancel_trigger_run",
        registry_tool_name="cancel_trigger_run",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Cancel/kill/stop a specific in-progress trigger run that is hanging. Call this directly "
            "for 'cancel it'/'stop the hanging run' requests about a trigger — do not substitute "
            "list_triggers or get_trigger_run_history, which only report state and cannot stop "
            "anything."
        ),
        params_json_schema=schema({
            "trigger_name": {"type": "string"},
            "trigger_run_id": {"type": "string"},
            "reason": REASON_PROP,
        }, ["trigger_name", "trigger_run_id", "reason"]),
    ),
]
