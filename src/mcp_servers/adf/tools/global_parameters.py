from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import GlobalParameterResource

from mcp_servers.adf.tools._shared import (
    _client,
    _reject_if_dropped_fields,
    _reject_if_miscased,
    _to_wire_dict,
)

# ADF only allows ONE Global Parameters resource per factory, always named "default" (any
# other name raises GlobalParameterNameNotAllowed) — individual parameter names live as keys
# inside that single resource's `properties` dict, not as separate ADF resources. Every tool
# below reads/writes the whole "default" resource and patches one key in/out of its
# properties dict; there's no such thing as creating/deleting just one parameter's resource
# in isolation.
_DEFAULT_RESOURCE_NAME = "default"


def _get_all_properties(client, resource_group: str, factory_name: str) -> dict:
    """Current global parameters as {name: {"type":..., "value":...}}, {} if none exist yet."""
    try:
        resource = client.global_parameters.get(
            resource_group, factory_name, _DEFAULT_RESOURCE_NAME
        )
    except ResourceNotFoundError:
        return {}
    wire = _to_wire_dict(resource)
    return wire if isinstance(wire, dict) else {}


def _write_all_properties(
    client, resource_group: str, factory_name: str, properties: dict
):
    """
    Writes the full properties dict back as the singleton "default" resource. Returns
    (error_dict, None) on validation failure, else (None, updated_resource).
    """
    error = _reject_if_dropped_fields(
        {"properties": properties}, GlobalParameterResource, "global parameter"
    )
    if error:
        return error, None
    resource = GlobalParameterResource.deserialize({"properties": properties})
    error = _reject_if_miscased(resource, "global parameter")
    if error:
        return error, None
    updated = client.global_parameters.create_or_update(
        resource_group, factory_name, _DEFAULT_RESOURCE_NAME, resource
    )
    return None, updated


def list_global_parameters(
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Factory-wide global parameter sweep — name, type, and value for each. These are the
    factory-level parameters referenced by pipelines/datasets/linked services via
    @pipeline().globalParameters.<name>. Returns an empty list if the factory has no global
    parameters defined at all (there is no separate resource to create until the first one
    is added — see create_resource).
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    properties = _get_all_properties(client, resource_group, factory_name)
    return {
        "global_parameters": [
            {"name": name, "type": spec.get("type"), "value": spec.get("value")}
            for name, spec in properties.items()
        ]
    }


def get_global_parameter_definition_raw(
    global_parameter_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    Full global parameter definition ({"type": ..., "value": ...}). Feed the returned dict
    back into update_resource_definition (with edits applied) to apply a fix — e.g. a stale
    connection string or environment flag baked in as a global.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    properties = _get_all_properties(client, resource_group, factory_name)
    if global_parameter_name not in properties:
        return {
            "error": "global_parameter_not_found",
            "global_parameter_name": global_parameter_name,
        }
    return properties[global_parameter_name]


def create_global_parameter(
    global_parameter_name: str,
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
    Adds a brand-new global parameter. Fails with an explicit error if one with this name
    already exists — use update_resource_definition to modify an existing one instead.
    `definition` should be {"type": ..., "value": ...}. Under the hood this reads the
    factory's whole set of global parameters, adds this one, and writes the full set back.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    properties = _get_all_properties(client, resource_group, factory_name)
    if global_parameter_name in properties:
        return {
            "error": "global_parameter_already_exists",
            "global_parameter_name": global_parameter_name,
        }

    merged = {**properties, global_parameter_name: definition}
    error, updated = _write_all_properties(client, resource_group, factory_name, merged)
    if error:
        return error

    return {
        "global_parameter_name": global_parameter_name,
        "created": True,
        "reason": reason,
        "etag": updated.etag,
    }


def update_global_parameter_definition(
    global_parameter_name: str,
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
    Overwrites a global parameter's type/value. Fails with an explicit error if no parameter
    with this name exists yet — use create_resource first.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    properties = _get_all_properties(client, resource_group, factory_name)
    if global_parameter_name not in properties:
        return {
            "error": "global_parameter_not_found",
            "global_parameter_name": global_parameter_name,
        }

    merged = {**properties, global_parameter_name: definition}
    error, updated = _write_all_properties(client, resource_group, factory_name, merged)
    if error:
        return error

    return {
        "global_parameter_name": global_parameter_name,
        "updated": True,
        "reason": reason,
        "etag": updated.etag,
    }
