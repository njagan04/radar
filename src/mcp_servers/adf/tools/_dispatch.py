"""
Thin resource_type -> implementation dispatchers for tool operations that are identical
across resource kinds. Each dispatcher below is a consolidated replacement for what would
otherwise be N separate top-level tools (one per resource kind) — the actual Azure SDK call
logic in each kind's own module is untouched and reused as-is; only the public entry point
is consolidated.

Collapses only along the resource_type axis (same operation, every kind merged behind one
tool) — never across the mutating/read-only boundary. Each operation is uniformly sync or
async across every kind it covers (create/update/rollback/back/forward always touch the
checkpoint system, hence async; get_definition_raw/list never do, hence sync) — see
mcp_servers/adf/tools/_checkpoints.py's module docstring for why Postgres access is
necessarily async in this codebase.

"pipeline", "linked_service", "dataset", "data_flow", "global_parameter", and "trigger" are
now populated — every *_BY_KIND table below extends by adding one line per newly-ported
resource kind, not by touching this file's dispatch logic or the per-kind modules.
"""
from mcp_servers.adf.tools import data_flows, datasets, global_parameters, linked_services, pipelines, triggers

# Every kind's implementation functions take a differently-named "resource name" kwarg
# (pipeline_name, dataset_name, ...) even though the rest of their signature is identical —
# this is the one piece of per-kind knowledge every dispatcher below needs.
_NAME_KWARG_BY_KIND = {
    "pipeline": "pipeline_name",
    "dataset": "dataset_name",
    "linked_service": "service_name",
    "data_flow": "data_flow_name",
    "trigger": "trigger_name",
    "global_parameter": "global_parameter_name",
}


def _by_kind(fn_by_kind: dict) -> dict:
    """{kind: fn} -> {kind: (fn, that kind's name kwarg)}."""
    return {kind: (fn, _NAME_KWARG_BY_KIND[kind]) for kind, fn in fn_by_kind.items()}


def _unknown_kind_error(table: dict, resource_type: str) -> dict:
    return {
        "error": "unknown_resource_type",
        "resource_type": resource_type,
        "valid_resource_types": sorted(table),
    }


# get_resource_definition_raw: triggers have no equivalent raw-definition getter (deliberately
# excluded even once ported — see claude-desktop's schemas/__init__.py comment), so "trigger"
# is intentionally absent here even though it's populated in every other table below.
_GET_DEFINITION_RAW_BY_KIND = _by_kind({
    "pipeline": pipelines.get_pipeline_definition_raw,
    "linked_service": linked_services.get_linked_service_definition_raw,
    "dataset": datasets.get_dataset_definition_raw,
    "data_flow": data_flows.get_data_flow_definition_raw,
    "global_parameter": global_parameters.get_global_parameter_definition_raw,
})

_CREATE_BY_KIND = _by_kind({
    "pipeline": pipelines.create_pipeline,
    "linked_service": linked_services.create_linked_service,
    "dataset": datasets.create_dataset,
    "data_flow": data_flows.create_data_flow,
    "global_parameter": global_parameters.create_global_parameter,
    "trigger": triggers.create_trigger,
})

_LIST_BY_KIND = {
    "pipeline": pipelines.list_pipelines,
    "linked_service": linked_services.list_linked_services,
    "dataset": datasets.list_datasets,
    "data_flow": data_flows.list_data_flows,
    "global_parameter": global_parameters.list_global_parameters,
    "trigger": triggers.list_triggers,
}

_UPDATE_DEFINITION_BY_KIND = _by_kind({
    "pipeline": pipelines.update_pipeline_definition,
    "linked_service": linked_services.update_linked_service_definition,
    "dataset": datasets.update_dataset_definition,
    "data_flow": data_flows.update_data_flow_definition,
    "global_parameter": global_parameters.update_global_parameter_definition,
    "trigger": triggers.update_trigger_definition,
})

_LIST_SNAPSHOTS_BY_KIND = _by_kind({
    "pipeline": pipelines.list_pipeline_snapshots,
    "linked_service": linked_services.list_linked_service_snapshots,
    "dataset": datasets.list_dataset_snapshots,
    "data_flow": data_flows.list_data_flow_snapshots,
    "global_parameter": global_parameters.list_global_parameter_snapshots,
    "trigger": triggers.list_trigger_snapshots,
})

_ROLLBACK_BY_KIND = _by_kind({
    "pipeline": pipelines.rollback_pipeline_definition,
    "linked_service": linked_services.rollback_linked_service_definition,
    "dataset": datasets.rollback_dataset_definition,
    "data_flow": data_flows.rollback_data_flow_definition,
    "global_parameter": global_parameters.rollback_global_parameter_definition,
    "trigger": triggers.rollback_trigger_definition,
})

_BACK_BY_KIND = _by_kind({
    "pipeline": pipelines.back_pipeline_definition,
    "linked_service": linked_services.back_linked_service_definition,
    "dataset": datasets.back_dataset_definition,
    "data_flow": data_flows.back_data_flow_definition,
    "global_parameter": global_parameters.back_global_parameter_definition,
    "trigger": triggers.back_trigger_definition,
})

_FORWARD_BY_KIND = _by_kind({
    "pipeline": pipelines.forward_pipeline_definition,
    "linked_service": linked_services.forward_linked_service_definition,
    "dataset": datasets.forward_dataset_definition,
    "data_flow": data_flows.forward_data_flow_definition,
    "global_parameter": global_parameters.forward_global_parameter_definition,
    "trigger": triggers.forward_trigger_definition,
})


def get_resource_definition_raw(resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind get_*_definition_raw implementation. Sync — no
    checkpoint involvement."""
    try:
        fn, name_kwarg = _GET_DEFINITION_RAW_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_GET_DEFINITION_RAW_BY_KIND, resource_type)
    return fn(**{name_kwarg: name}, **kwargs)


async def create_resource(db, project: str, resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind create_* implementation. Async — every kind's create_*
    pushes checkpoints. `db`/`project` are gateway-level context injected by
    RBACGateway._dispatch(), not agent-supplied arguments — forwarded through unchanged."""
    try:
        fn, name_kwarg = _CREATE_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_CREATE_BY_KIND, resource_type)
    return await fn(db, project, **{name_kwarg: name}, **kwargs)


def list_resources(resource_type: str, **kwargs) -> dict:
    """Routes to the matching per-kind list_* implementation. No resource name to remap."""
    try:
        fn = _LIST_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_LIST_BY_KIND, resource_type)
    return fn(**kwargs)


async def update_resource_definition(db, project: str, resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind update_*_definition implementation. Async."""
    try:
        fn, name_kwarg = _UPDATE_DEFINITION_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_UPDATE_DEFINITION_BY_KIND, resource_type)
    return await fn(db, project, **{name_kwarg: name}, **kwargs)


async def list_resource_snapshots(db, project: str, resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind list_*_snapshots implementation. Async — reads the
    checkpoint history table."""
    try:
        fn, name_kwarg = _LIST_SNAPSHOTS_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_LIST_SNAPSHOTS_BY_KIND, resource_type)
    return await fn(db, project, **{name_kwarg: name}, **kwargs)


async def rollback_resource_definition(db, project: str, resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind rollback_*_definition implementation. Async."""
    try:
        fn, name_kwarg = _ROLLBACK_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_ROLLBACK_BY_KIND, resource_type)
    return await fn(db, project, **{name_kwarg: name}, **kwargs)


async def back_resource_definition(db, project: str, resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind back_*_definition implementation. Async."""
    try:
        fn, name_kwarg = _BACK_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_BACK_BY_KIND, resource_type)
    return await fn(db, project, **{name_kwarg: name}, **kwargs)


async def forward_resource_definition(db, project: str, resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind forward_*_definition implementation. Async."""
    try:
        fn, name_kwarg = _FORWARD_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_FORWARD_BY_KIND, resource_type)
    return await fn(db, project, **{name_kwarg: name}, **kwargs)
