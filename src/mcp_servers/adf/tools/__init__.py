"""
Single source of truth for the ADF tool registry. Imported by gateway/rbac.py (the sole
in-process RBAC-gated dispatch path — the earlier separate stdio MCP server, server.py, was
deleted entirely as unused and RBAC-bypassing).

TOOL_REGISTRY has exactly 68 entries, one per mcp_servers.adf.schemas.SPECS entry — matching
both RBAC layers (gateway/rbac.py's execution-time _check_permission and tool_search_tool.py's
SDK-level needs_approval) to the same per-kind granularity (2026-07-30). Two families:
  - 21 tools with no cross-kind equivalent (pipeline run/error/history/rerun/cancel, trigger
    run/start/stop/history/rerun/cancel, get_linked_service, get_dataset_definition,
    get_data_flow_definition, integration-runtime status/start) are direct entries, registered
    exactly once, same as always.
  - 47 tools (create/update/rollback/back/forward/list/get_definition_raw/list_snapshots across
    pipeline/dataset/linked_service/data_flow/trigger/global_parameter) share just 8 real
    Azure-calling implementations in _dispatch.py — no code duplication — but each gets its own
    real registry entry here via a thin wrapper that pre-binds resource_type, generated from
    SPECS below rather than hand-listed (self-maintaining if a new kind/operation is added).
    Previously these 47 were only reachable through the 8 generic names directly (e.g.
    "create_resource"); those 8 names are no longer registered at all — nothing calls them by
    that name anymore now that retrieval.py dispatches by the real per-kind tool name.
"""
import asyncio
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
}

# The 8 real Azure-calling implementations shared across resource kinds — not registered
# under these generic names directly (see module docstring); only used below to build each
# kind's own thin wrapper.
_GENERIC_IMPLS: dict[str, Callable[..., dict]] = {
    "get_resource_definition_raw": dispatch.get_resource_definition_raw,
    "create_resource": dispatch.create_resource,
    "list_resources": dispatch.list_resources,
    "update_resource_definition": dispatch.update_resource_definition,
    "list_resource_snapshots": dispatch.list_resource_snapshots,
    "rollback_resource_definition": dispatch.rollback_resource_definition,
    "back_resource_definition": dispatch.back_resource_definition,
    "forward_resource_definition": dispatch.forward_resource_definition,
}


def _bind_resource_type(fn: Callable[..., dict], resource_type: str) -> Callable[..., dict]:
    """Pre-binds resource_type so each per-kind tool name has its own real registry entry,
    while still calling the one shared implementation for that operation. Preserves
    sync-vs-async — RBACGateway._dispatch() branches on asyncio.iscoroutinefunction(fn)."""
    if asyncio.iscoroutinefunction(fn):
        async def _wrapped_async(**kwargs):
            return await fn(resource_type=resource_type, **kwargs)
        return _wrapped_async

    def _wrapped_sync(**kwargs):
        return fn(resource_type=resource_type, **kwargs)
    return _wrapped_sync


for _spec in SPECS:
    if _spec.name in TOOL_REGISTRY:
        continue
    # Every spec not already a direct entry is generic-dispatch-derived, per
    # ADFToolSpec's own contract (base.py: resource_type is set exactly when
    # registry_tool_name points at a shared generic implementation).
    assert _spec.resource_type is not None, f"{_spec.name} has no resource_type to bind"
    TOOL_REGISTRY[_spec.name] = _bind_resource_type(
        _GENERIC_IMPLS[_spec.registry_tool_name], _spec.resource_type
    )
