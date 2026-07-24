"""
Single source of truth for the ADF tool registry. Imported by both server.py (stdio MCP
path) and rbac.py (in-process RBAC gateway path).

Three families of tool, matching claude-desktop/mcp_adf's schema split exactly:
  - Pipeline-specific/trigger-specific tools (run/error/history/rerun/cancel/start/stop — the
    10 pipeline + 6 trigger tools in claude-desktop/mcp_adf/schemas/pipelines.py and
    schemas/triggers.py) are direct top-level entries here, one per operation, since they
    have no equivalent across other resource kinds.
  - Generic resource_type-parameterized tools (create/update/rollback/back/forward/list/
    get_definition_raw/list_snapshots — the 8 in .../schemas/_generic.py) are registered
    ONCE each, dispatched by resource_type via _dispatch.py — NOT also registered under
    per-kind names like "create_pipeline". Confirmed against the original: schemas/
    pipelines.py's 10-tool list deliberately excludes create_pipeline/update_pipeline_
    definition/list_pipelines/get_pipeline_definition_raw/list_pipeline_snapshots/rollback_
    back_forward_pipeline_definition — those are reachable only through the generic tools.
  - Integration-runtime tools (get_integration_runtime_status/start_integration_runtime — the
    2 in schemas/integration_runtimes.py) are also direct top-level entries: an integration
    runtime has no versionable definition to snapshot/rollback, so they never touch
    _checkpoints.py or _dispatch.py at all — the only resource kind entirely outside the
    checkpoint system.

This completes the full 27-tool port from claude-desktop/mcp_adf/ across all 7 resource
kinds (pipeline/linked_service/dataset/data_flow/global_parameter/trigger/
integration_runtime).
"""
from collections.abc import Callable

from mcp_servers.adf.tools import _dispatch as dispatch
from mcp_servers.adf.tools.integration_runtimes import (
    get_integration_runtime_status,
    start_integration_runtime,
)
from mcp_servers.adf.tools.linked_services import get_linked_service
from mcp_servers.adf.tools.pipelines import (
    cancel_pipeline_run,
    get_activity_run_error,
    get_activity_run_history,
    get_activity_run_io,
    get_pipeline_definition,
    get_pipeline_run_history,
    get_pipeline_run_status,
    list_activity_runs,
    list_pipeline_runs,
    rerun_pipeline,
)
from mcp_servers.adf.tools.triggers import (
    cancel_trigger_run,
    get_trigger,
    get_trigger_run_history,
    rerun_trigger_run,
    start_trigger,
    stop_trigger,
)

TOOL_REGISTRY: dict[str, Callable[..., dict]] = {
    # Pipeline-specific (direct top-level entries — no other kind has an equivalent)
    "get_activity_run_error": get_activity_run_error,
    "get_pipeline_run_status": get_pipeline_run_status,
    "get_pipeline_run_history": get_pipeline_run_history,
    "get_activity_run_history": get_activity_run_history,
    "get_pipeline_definition": get_pipeline_definition,
    "rerun_pipeline": rerun_pipeline,
    "list_activity_runs": list_activity_runs,
    "get_activity_run_io": get_activity_run_io,
    "list_pipeline_runs": list_pipeline_runs,
    "cancel_pipeline_run": cancel_pipeline_run,
    # Linked-service-specific
    "get_linked_service": get_linked_service,
    # Trigger-specific (direct top-level entries — get_trigger reports only runtime state,
    # not a full definition, so it's not the same shape as get_resource_definition_raw; start/
    # stop are runtime-state transitions with no checkpoint history; run history/rerun/cancel
    # operate on trigger RUNS, a different concept from pipeline runs)
    "get_trigger": get_trigger,
    "start_trigger": start_trigger,
    "stop_trigger": stop_trigger,
    "get_trigger_run_history": get_trigger_run_history,
    "rerun_trigger_run": rerun_trigger_run,
    "cancel_trigger_run": cancel_trigger_run,
    # Integration-runtime-specific (direct top-level entries — the only kind with no
    # checkpoint/dispatch involvement at all; no versionable definition exists to snapshot)
    "get_integration_runtime_status": get_integration_runtime_status,
    "start_integration_runtime": start_integration_runtime,
    # Generic, resource_type-dispatched (covers pipeline/linked_service/dataset/data_flow/
    # global_parameter/trigger; each new kind ported adds itself to _dispatch.py's tables)
    "get_resource_definition_raw": dispatch.get_resource_definition_raw,
    "create_resource": dispatch.create_resource,
    "list_resources": dispatch.list_resources,
    "update_resource_definition": dispatch.update_resource_definition,
    "list_resource_snapshots": dispatch.list_resource_snapshots,
    "rollback_resource_definition": dispatch.rollback_resource_definition,
    "back_resource_definition": dispatch.back_resource_definition,
    "forward_resource_definition": dispatch.forward_resource_definition,
}
