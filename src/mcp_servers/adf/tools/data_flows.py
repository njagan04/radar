from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DataFlowResource

from mcp_servers.adf.tools._shared import (
    _client,
    _reject_if_dropped_fields,
    _reject_if_miscased,
    _to_wire_dict,
)


def list_data_flows(
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Factory-wide data flow sweep — name and type (e.g. MappingDataFlow) for each."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    data_flows = client.data_flows.list_by_factory(resource_group, factory_name)
    return {
        "data_flows": [
            {
                "name": df.name,
                "type": df.properties.type if df.properties else "unknown",
            }
            for df in data_flows
        ]
    }


def get_data_flow_definition(
    data_flow_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Type and the names of its sources/sinks/transformations only, not the full
    transformation script. Use get_data_flow_definition_raw to see the actual
    transformation logic.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    data_flow = client.data_flows.get(resource_group, factory_name, data_flow_name)
    wire = _to_wire_dict(data_flow)
    type_properties = wire.get("typeProperties") or {}
    return {
        "name": data_flow_name,
        "type": wire.get("type", "unknown"),
        "sources": [s.get("name") for s in type_properties.get("sources") or []],
        "sinks": [s.get("name") for s in type_properties.get("sinks") or []],
        "transformations": [
            t.get("name") for t in type_properties.get("transformations") or []
        ],
    }


def get_data_flow_definition_raw(
    data_flow_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Full Mapping Data Flow definition (sources, sinks, transformation script) — the only
    way to see the transformation graph itself, since pipeline definitions only show that
    an activity references a data flow by name. The editable structure to feed back into
    update_resource_definition.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    data_flow = client.data_flows.get(resource_group, factory_name, data_flow_name)
    return _to_wire_dict(data_flow)


def create_data_flow(
    data_flow_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    definition: dict,
    reason: str,
) -> dict:
    """
    Creates a brand-new data flow. Fails with an explicit error if a data flow with this
    name already exists — use update_resource_definition to modify an existing one instead.

    `definition` accepts either the flat shape get_data_flow_definition_raw uses, or the
    ARM/Studio-export shape ({"name": ..., "properties": {...}}) — if a "properties" key is
    present, its contents are used and the wrapper is discarded. `data_flow_name` always
    determines the actual name created.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    try:
        client.data_flows.get(resource_group, factory_name, data_flow_name)
        return {"error": "data_flow_already_exists", "data_flow_name": data_flow_name}
    except ResourceNotFoundError:
        pass

    properties = definition.get("properties", definition)
    error = _reject_if_dropped_fields(
        {"properties": properties}, DataFlowResource, "data flow"
    )
    if error:
        return error
    data_flow_resource = DataFlowResource.deserialize({"properties": properties})
    error = _reject_if_miscased(data_flow_resource, "data flow")
    if error:
        return error
    created = client.data_flows.create_or_update(
        resource_group, factory_name, data_flow_name, data_flow_resource
    )

    return {
        "data_flow_name": data_flow_name,
        "created": True,
        "reason": reason,
        "etag": created.etag,
    }


def update_data_flow_definition(
    data_flow_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    definition: dict,
    reason: str,
) -> dict:
    """Overwrites a data flow's full definition."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    error = _reject_if_dropped_fields(
        {"properties": definition}, DataFlowResource, "data flow"
    )
    if error:
        return error
    data_flow_resource = DataFlowResource.deserialize({"properties": definition})
    error = _reject_if_miscased(data_flow_resource, "data flow")
    if error:
        return error
    updated = client.data_flows.create_or_update(
        resource_group, factory_name, data_flow_name, data_flow_resource
    )

    return {
        "data_flow_name": data_flow_name,
        "updated": True,
        "reason": reason,
        "etag": updated.etag,
    }
