from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DatasetResource

from mcp_servers.adf.tools._shared import (
    _client,
    _reject_if_dropped_fields,
    _reject_if_miscased,
    _to_wire_dict,
)


def list_datasets(
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Factory-wide dataset sweep — name, type, and backing linked service for each."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    datasets = client.datasets.list_by_factory(resource_group, factory_name)
    return {
        "datasets": [
            {
                "name": d.name,
                "type": d.properties.type if d.properties else "unknown",
                "linked_service_name": (
                    getattr(d.properties.linked_service_name, "reference_name", None)
                    if d.properties and d.properties.linked_service_name
                    else None
                ),
            }
            for d in datasets
        ]
    }


def get_dataset_definition(
    dataset_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Type, backing linked service, and declared schema/column names only. Omits parameters,
    folder, annotations, and other wire-format metadata. Use get_dataset_definition_raw for
    the full editable structure.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    dataset = client.datasets.get(resource_group, factory_name, dataset_name)
    wire = _to_wire_dict(dataset)
    return {
        "name": dataset_name,
        "type": wire.get("type", "unknown"),
        "linked_service_name": (wire.get("linkedServiceName") or {}).get(
            "referenceName"
        ),
        "schema": wire.get("schema") or wire.get("structure"),
    }


def get_dataset_definition_raw(
    dataset_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Full dataset definition: schema, structure, linked service reference, parameters. The
    editable structure to feed back into update_resource_definition.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    dataset = client.datasets.get(resource_group, factory_name, dataset_name)
    return _to_wire_dict(dataset)


def create_dataset(
    dataset_name: str,
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
    Creates a brand-new dataset. Fails with an explicit error if a dataset with this name
    already exists — use update_resource_definition to modify an existing one instead.

    `definition` accepts either the flat shape get_dataset_definition_raw uses, or the ARM/
    Studio-export shape ({"name": ..., "properties": {...}}) — if a "properties" key is
    present, its contents are used and the wrapper is discarded. `dataset_name` always
    determines the actual name created.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    try:
        client.datasets.get(resource_group, factory_name, dataset_name)
        return {"error": "dataset_already_exists", "dataset_name": dataset_name}
    except ResourceNotFoundError:
        pass

    properties = definition.get("properties", definition)
    error = _reject_if_dropped_fields(
        {"properties": properties}, DatasetResource, "dataset"
    )
    if error:
        return error
    dataset_resource = DatasetResource.deserialize({"properties": properties})
    error = _reject_if_miscased(dataset_resource, "dataset")
    if error:
        return error
    created = client.datasets.create_or_update(
        resource_group, factory_name, dataset_name, dataset_resource
    )

    return {
        "dataset_name": dataset_name,
        "created": True,
        "reason": reason,
        "etag": created.etag,
    }


def update_dataset_definition(
    dataset_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    definition: dict,
    reason: str,
) -> dict:
    """Overwrites a dataset's full definition (e.g. to correct a drifted schema)."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    error = _reject_if_dropped_fields(
        {"properties": definition}, DatasetResource, "dataset"
    )
    if error:
        return error
    dataset_resource = DatasetResource.deserialize({"properties": definition})
    error = _reject_if_miscased(dataset_resource, "dataset")
    if error:
        return error
    updated = client.datasets.create_or_update(
        resource_group, factory_name, dataset_name, dataset_resource
    )

    return {
        "dataset_name": dataset_name,
        "updated": True,
        "reason": reason,
        "etag": updated.etag,
    }
