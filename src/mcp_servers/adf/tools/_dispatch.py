"""
Thin resource_type -> implementation dispatchers for tool operations that are identical
across resource kinds. Each dispatcher routes to the real per-kind implementation in that
kind's own module; adding a new resource kind means adding one line per *_BY_KIND table
here, not touching the dispatch logic itself.

All operations here are synchronous; the ADF Azure resources already carry their own
versioning/publish history.
"""

from mcp_servers.adf.tools import (
    data_flows,
    datasets,
    global_parameters,
    linked_services,
    pipelines,
    triggers,
)

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


# Triggers have no equivalent raw-definition getter, so "trigger" is intentionally absent
# here even though it's populated in every other table below.
_GET_DEFINITION_RAW_BY_KIND = _by_kind(
    {
        "pipeline": pipelines.get_pipeline_definition_raw,
        "linked_service": linked_services.get_linked_service_definition_raw,
        "dataset": datasets.get_dataset_definition_raw,
        "data_flow": data_flows.get_data_flow_definition_raw,
        "global_parameter": global_parameters.get_global_parameter_definition_raw,
    }
)

_CREATE_BY_KIND = _by_kind(
    {
        "pipeline": pipelines.create_pipeline,
        "linked_service": linked_services.create_linked_service,
        "dataset": datasets.create_dataset,
        "data_flow": data_flows.create_data_flow,
        "global_parameter": global_parameters.create_global_parameter,
        "trigger": triggers.create_trigger,
    }
)

_LIST_BY_KIND = {
    "pipeline": pipelines.list_pipelines,
    "linked_service": linked_services.list_linked_services,
    "dataset": datasets.list_datasets,
    "data_flow": data_flows.list_data_flows,
    "global_parameter": global_parameters.list_global_parameters,
    "trigger": triggers.list_triggers,
}

_UPDATE_DEFINITION_BY_KIND = _by_kind(
    {
        "pipeline": pipelines.update_pipeline_definition,
        "linked_service": linked_services.update_linked_service_definition,
        "dataset": datasets.update_dataset_definition,
        "data_flow": data_flows.update_data_flow_definition,
        "global_parameter": global_parameters.update_global_parameter_definition,
        "trigger": triggers.update_trigger_definition,
    }
)


def get_resource_definition_raw(resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind get_*_definition_raw implementation."""
    try:
        fn, name_kwarg = _GET_DEFINITION_RAW_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_GET_DEFINITION_RAW_BY_KIND, resource_type)
    return fn(**{name_kwarg: name}, **kwargs)


def create_resource(resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind create_* implementation."""
    try:
        fn, name_kwarg = _CREATE_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_CREATE_BY_KIND, resource_type)
    return fn(**{name_kwarg: name}, **kwargs)


def list_resources(resource_type: str, **kwargs) -> dict:
    """Routes to the matching per-kind list_* implementation. No resource name to remap."""
    try:
        fn = _LIST_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_LIST_BY_KIND, resource_type)
    return fn(**kwargs)


def update_resource_definition(resource_type: str, name: str, **kwargs) -> dict:
    """Routes to the matching per-kind update_*_definition implementation."""
    try:
        fn, name_kwarg = _UPDATE_DEFINITION_BY_KIND[resource_type]
    except KeyError:
        return _unknown_kind_error(_UPDATE_DEFINITION_BY_KIND, resource_type)
    return fn(**{name_kwarg: name}, **kwargs)
