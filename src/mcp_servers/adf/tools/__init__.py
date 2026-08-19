"""
Single source of truth for the ADF tool registry. Imported by gateway/rbac.py, the sole
in-process RBAC-gated dispatch path.

TOOL_REGISTRY has one entry per mcp_servers.adf.schemas.SPECS entry, matching both RBAC
layers (gateway/rbac.py's execution-time _check_permission and tool_search_tool.py's SDK-level
needs_approval) to the same per-kind granularity. Two families:
  - Tools with no cross-kind equivalent (pipeline run/error/history/rerun/cancel, trigger
    run/start/stop/history/rerun/cancel, get_linked_service, get_dataset_definition,
    get_data_flow_definition, integration-runtime status/start) are direct entries.
  - Tools (create/update/list/get_definition_raw across pipeline/dataset/linked_service/
    data_flow/trigger/global_parameter) share 4 real Azure-calling implementations in
    _dispatch.py; each gets its own registry entry here via a thin wrapper that pre-binds
    resource_type, generated from SPECS below rather than hand-listed.

ADF's own Azure resources carry their own publish/version history, so no separate
versioning system is needed here.
"""

from collections.abc import Callable

from mcp_servers.adf.schemas import SPECS
from mcp_servers.adf.tools import _dispatch as dispatch
from mcp_servers.adf.tools.data_flows import get_data_flow_definition
from mcp_servers.adf.tools.datasets import get_dataset_definition
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
    # Dataset-specific (cheap read-only summary, see schemas/datasets.py)
    "get_dataset_definition": get_dataset_definition,
    # Data-flow-specific (cheap read-only summary, see schemas/data_flows.py)
    "get_data_flow_definition": get_data_flow_definition,
    # Linked-service-specific
    "get_linked_service": get_linked_service,
    # Trigger-specific: get_trigger reports only runtime state, not a full definition, so
    # it's not the same shape as get_resource_definition_raw. Run history/rerun/cancel
    # operate on trigger RUNS, a different concept from pipeline runs.
    "get_trigger": get_trigger,
    "start_trigger": start_trigger,
    "stop_trigger": stop_trigger,
    "get_trigger_run_history": get_trigger_run_history,
    "rerun_trigger_run": rerun_trigger_run,
    "cancel_trigger_run": cancel_trigger_run,
    # Integration-runtime-specific: no versionable definition exists for this kind.
    "get_integration_runtime_status": get_integration_runtime_status,
    "start_integration_runtime": start_integration_runtime,
}

# The 4 real Azure-calling implementations shared across resource kinds — not registered
# under these generic names directly; only used below to build each kind's own thin wrapper.
_GENERIC_IMPLS: dict[str, Callable[..., dict]] = {
    "get_resource_definition_raw": dispatch.get_resource_definition_raw,
    "create_resource": dispatch.create_resource,
    "list_resources": dispatch.list_resources,
    "update_resource_definition": dispatch.update_resource_definition,
}


def _bind_resource_type(
    fn: Callable[..., dict], resource_type: str
) -> Callable[..., dict]:
    """Pre-binds resource_type so each per-kind tool name has its own real registry entry,
    while still calling the one shared implementation for that operation."""

    def _wrapped(**kwargs):
        return fn(resource_type=resource_type, **kwargs)

    return _wrapped


for _spec in SPECS:
    if _spec.name in TOOL_REGISTRY:
        continue
    # Every spec not already a direct entry is generic-dispatch-derived: resource_type is
    # set exactly when registry_tool_name points at a shared generic implementation.
    assert _spec.resource_type is not None, f"{_spec.name} has no resource_type to bind"
    TOOL_REGISTRY[_spec.name] = _bind_resource_type(
        _GENERIC_IMPLS[_spec.registry_tool_name], _spec.resource_type
    )
