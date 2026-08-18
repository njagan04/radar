"""
Pipeline tool specs — mirrors claude-desktop/mcp_adf/schemas/pipelines.py's per-kind layout
(one file per resource kind, matching mcp_servers/adf/tools/pipelines.py's own naming), not a
cross-kind generic loop. Each ADFToolSpec here is either one of the 8 generic-dispatch
operations (create/list/update/rollback/back/forward/list_snapshots/get_definition_raw, all
routed through TOOL_REGISTRY's resource_type-parameterized functions) or one of pipeline's own
10 always-distinct tools (registry_tool_name == name, resource_type=None).
"""

from mcp_servers.adf.schemas.base import REASON_PROP, ADFToolSpec, schema

TOOLS = [
    ADFToolSpec(
        name="get_pipeline_definition_raw",
        registry_tool_name="get_resource_definition_raw",
        resource_type="pipeline",
        name_kwarg="pipeline_name",
        description=(
            "Full pipeline definition JSON — every activity's typeProperties (dataset/linked-service "
            "references via inputs/outputs/linkedServiceName, queries like sqlReaderQuery, source/sink "
            "settings), policy (timeout, retry, retryIntervalInSeconds), and dependsOn. Use this during "
            "DIAGNOSIS, not just before writing a fix: after get_activity_run_error identifies the failing "
            "activity, call this to see its actual timeout value, the query it ran, or which dataset/linked "
            "service it touches — usually the concrete evidence for WHY it failed. It's also the editable "
            "structure required as input to update_pipeline_definition once a fix is decided."
        ),
        params_json_schema=schema(
            {"pipeline_name": {"type": "string"}}, ["pipeline_name"]
        ),
    ),
    ADFToolSpec(
        name="create_pipeline",
        registry_tool_name="create_resource",
        resource_type="pipeline",
        name_kwarg="pipeline_name",
        description=(
            "Creates a brand-new pipeline. Fails with an explicit error if a pipeline with this name "
            "already exists — use update_pipeline_definition to modify an existing one instead. "
            "`definition` accepts either the flat shape "
            "get_pipeline_definition_raw uses, or the ARM/Data-Factory-Studio export shape "
            '({"name": ..., "properties": {"activities": [...], ...}}) — if a "properties" key is '
            "present, its contents are used and the wrapper is discarded. `pipeline_name` (not the JSON's "
            'own "name" field, if present) determines the actual name created.'
        ),
        params_json_schema=schema(
            {
                "pipeline_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Pipeline definition JSON.",
                },
                "reason": REASON_PROP,
            },
            ["pipeline_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="list_pipelines",
        registry_tool_name="list_resources",
        resource_type="pipeline",
        name_kwarg=None,
        description="List all pipelines in the data factory.",
        params_json_schema=schema({}, []),
    ),
    ADFToolSpec(
        name="update_pipeline_definition",
        registry_tool_name="update_resource_definition",
        resource_type="pipeline",
        name_kwarg="pipeline_name",
        description=(
            "Overwrites a pipeline's full definition to apply a concrete fix (e.g. inserting a Wait "
            "activity, adjusting a timeout/retry policy). ADF has no partial-patch API — this replaces the "
            "entire activities array, so `definition` must be get_pipeline_definition_raw's output with "
            "edits applied."
        ),
        params_json_schema=schema(
            {
                "pipeline_name": {"type": "string"},
                "definition": {
                    "type": "object",
                    "description": "Modified output of get_pipeline_definition_raw.",
                },
                "reason": REASON_PROP,
            },
            ["pipeline_name", "definition", "reason"],
        ),
    ),
    ADFToolSpec(
        name="get_activity_run_error",
        registry_tool_name="get_activity_run_error",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Fetch the concrete error message/failure detail for a specific failed activity run — "
            "call this directly when asked why something failed or to pull up the error. Different "
            "from get_activity_run_history (aggregated failure-count summary, no error text) and "
            "list_activity_runs (lightweight listing, no error detail)."
        ),
        params_json_schema=schema(
            {
                "pipeline_name": {"type": "string"},
                "event_timestamp": {
                    "type": "string",
                    "description": "ISO-8601 timestamp from the failure event",
                },
            },
            ["pipeline_name", "event_timestamp"],
        ),
    ),
    ADFToolSpec(
        name="get_pipeline_run_status",
        registry_tool_name="get_pipeline_run_status",
        resource_type=None,
        name_kwarg=None,
        description="Get current status of a specific pipeline run (used for freshness check before rerun).",
        params_json_schema=schema({"run_id": {"type": "string"}}, ["run_id"]),
    ),
    ADFToolSpec(
        name="get_pipeline_run_history",
        registry_tool_name="get_pipeline_run_history",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Fetch recent run history for a pipeline — pipeline-level (when it ran, overall "
            "success/fail per run), not activity-level."
        ),
        params_json_schema=schema(
            {
                "pipeline_name": {"type": "string"},
                "days": {"type": "integer", "default": 7},
            },
            ["pipeline_name"],
        ),
    ),
    ADFToolSpec(
        name="get_activity_run_history",
        registry_tool_name="get_activity_run_history",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Aggregated summary of which activities have failed in recent runs of a pipeline. Returns "
            "failure counts and last error code per activity — useful for spotting recurring failures. "
            "Does NOT return the actual error text for a failure — use get_activity_run_error for that."
        ),
        params_json_schema=schema(
            {
                "pipeline_name": {"type": "string"},
                "days": {"type": "integer", "default": 7},
            },
            ["pipeline_name"],
        ),
    ),
    ADFToolSpec(
        name="get_pipeline_definition",
        registry_tool_name="get_pipeline_definition",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Fetch the pipeline definition (activity graph) — activity names, types, and "
            "ExecutePipeline references only. For a failing activity's actual timeout policy, the "
            "dataset/linked service it reads or writes, or a query/expression it runs, use "
            "get_pipeline_definition_raw instead."
        ),
        params_json_schema=schema(
            {"pipeline_name": {"type": "string"}}, ["pipeline_name"]
        ),
    ),
    ADFToolSpec(
        name="rerun_pipeline",
        registry_tool_name="rerun_pipeline",
        resource_type=None,
        name_kwarg=None,
        description="Trigger a new run of a pipeline.",
        params_json_schema=schema(
            {
                "pipeline_name": {"type": "string"},
                "parameters": {"type": "object"},
            },
            ["pipeline_name"],
        ),
    ),
    ADFToolSpec(
        name="list_activity_runs",
        registry_tool_name="list_activity_runs",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Lists every activity in a specific pipeline run — name, type, status, timing, and "
            "activity_run_id for each. Deliberately lightweight: does NOT include input/output — call "
            "get_activity_run_io with a specific activity_run_id from this list for that."
        ),
        params_json_schema=schema(
            {
                "run_id": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
            ["run_id"],
        ),
    ),
    ADFToolSpec(
        name="get_activity_run_io",
        registry_tool_name="get_activity_run_io",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Raw input/output payload for one specific activity run — not aggregated, not just the "
            "error, but the actual resolved input parameters and captured output ADF recorded for that "
            "exact execution."
        ),
        params_json_schema=schema(
            {
                "activity_run_id": {"type": "string"},
                "run_id": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
            ["activity_run_id", "run_id"],
        ),
    ),
    ADFToolSpec(
        name="list_pipeline_runs",
        registry_tool_name="list_pipeline_runs",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Factory-wide run sweep across every pipeline in a time window. Use this instead of "
            "calling get_pipeline_run_history once per pipeline when the question is about recent "
            "activity across the whole factory rather than one specific pipeline's history."
        ),
        params_json_schema=schema({"hours": {"type": "integer", "default": 24}}, []),
    ),
    ADFToolSpec(
        name="cancel_pipeline_run",
        registry_tool_name="cancel_pipeline_run",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Cancel/kill/stop a specific pipeline run that is currently in progress or stuck. Call "
            "this directly when the user wants a running execution stopped — do not call "
            "list_pipeline_runs first to search for it."
        ),
        params_json_schema=schema(
            {"run_id": {"type": "string"}, "reason": REASON_PROP}, ["run_id", "reason"]
        ),
    ),
]
