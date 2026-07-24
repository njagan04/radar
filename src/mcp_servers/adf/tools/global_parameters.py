import asyncio

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import GlobalParameterResource
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.adf.tools import _checkpoints as ck
from mcp_servers.adf.tools._shared import _client, _reject_if_dropped_fields, _reject_if_miscased, _to_wire_dict

# ADF only allows ONE Global Parameters resource per factory, always named "default" (any
# other name raises GlobalParameterNameNotAllowed) — individual parameter names live as keys
# inside that single resource's `properties` dict, not as separate ADF resources. Every tool
# below reads/writes the whole "default" resource and patches one key in/out of its
# properties dict; there's no such thing as creating/deleting just one parameter's resource
# in isolation. The checkpoint history below is keyed by (kind, global_parameter_name) purely
# for this codebase's own bookkeeping, independent of what the real Azure resource is named.
#
# Kind-naming note (same trap caught in data_flows.py, checked against models.py's
# ck_resource_snapshots_kind CHECK constraint before writing this file): claude-desktop's
# original uses bare "globalparameter" (no underscore); Postgres requires "global_parameter".
_KIND = "global_parameter"
_DEFAULT_RESOURCE_NAME = "default"


def _get_all_properties(client, resource_group: str, factory_name: str) -> dict:
    """Current global parameters as {name: {"type":..., "value":...}}, {} if none exist yet."""
    try:
        resource = client.global_parameters.get(resource_group, factory_name, _DEFAULT_RESOURCE_NAME)
    except ResourceNotFoundError:
        return {}
    wire = _to_wire_dict(resource)
    return wire if isinstance(wire, dict) else {}


def _write_all_properties(client, resource_group: str, factory_name: str, properties: dict):
    """
    Writes the full properties dict back as the singleton "default" resource. Returns
    (error_dict, None) on validation failure, else (None, updated_resource).
    """
    error = _reject_if_dropped_fields({"properties": properties}, GlobalParameterResource, "global parameter")
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
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
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
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Full global parameter definition ({"type": ..., "value": ...}). Feed the returned dict
    back into update_resource_definition (with edits applied) to apply a fix — e.g. a stale
    connection string or environment flag baked in as a global.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    properties = _get_all_properties(client, resource_group, factory_name)
    if global_parameter_name not in properties:
        return {"error": "global_parameter_not_found", "global_parameter_name": global_parameter_name}
    return properties[global_parameter_name]


# --- Checkpoint-enabled tools below — async, see pipelines.py's module comment for why.
# Unlike every other kind, create/update/rollback/back/forward here don't call
# client.global_parameters.create_or_update on a per-resource object directly — they read
# the whole factory-wide properties dict, patch in/out this one key, and write the whole
# dict back, since ADF itself has no per-parameter resource to address. ---

async def create_global_parameter(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    state_name: str | None = None,
) -> dict:
    """
    Adds a brand-new global parameter. Fails with an explicit error if one with this name
    already exists — use update_resource_definition to modify an existing one instead.
    Records two checkpoints: "before-creation" and one for the just-created content, named
    `state_name` if given (default "created"). `definition` should be
    {"type": ..., "value": ...}. Under the hood this reads the factory's whole set of global
    parameters, adds this one, and writes the full set back.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    properties = await loop.run_in_executor(None, lambda: _get_all_properties(client, resource_group, factory_name))
    if global_parameter_name in properties:
        return {"error": "global_parameter_already_exists", "global_parameter_name": global_parameter_name}

    await ck._push_snapshot(
        db, project, _KIND, global_parameter_name,
        state_name="before-creation", action="create", reason=reason,
        change_summary="global parameter did not exist",
    )

    merged = {**properties, global_parameter_name: definition}
    error, updated = await loop.run_in_executor(
        None, lambda: _write_all_properties(client, resource_group, factory_name, merged)
    )
    if error:
        return error
    updated_properties = _to_wire_dict(updated)

    saved = await ck._push_snapshot(
        db, project, _KIND, global_parameter_name,
        state_name=state_name or "created", action="exists", reason=reason,
        change_summary="global parameter created", definition=updated_properties.get(global_parameter_name),
    )
    await db.commit()
    return {
        "global_parameter_name": global_parameter_name,
        "created": True,
        "reason": reason,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def update_global_parameter_definition(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    change_summary: str,
    state_name: str | None = None,
) -> dict:
    """
    Overwrites a global parameter's type/value. Fails with an explicit error if no parameter
    with this name exists yet — use create_resource first. If this parameter has no history
    yet, its as-found content is captured as an "initial" checkpoint first.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    properties = await loop.run_in_executor(None, lambda: _get_all_properties(client, resource_group, factory_name))
    if global_parameter_name not in properties:
        return {"error": "global_parameter_not_found", "global_parameter_name": global_parameter_name}

    await ck._ensure_baseline(db, project, _KIND, global_parameter_name, properties[global_parameter_name], reason)

    merged = {**properties, global_parameter_name: definition}
    error, updated = await loop.run_in_executor(
        None, lambda: _write_all_properties(client, resource_group, factory_name, merged)
    )
    if error:
        return error
    updated_properties = _to_wire_dict(updated)

    saved = await ck._push_snapshot(
        db, project, _KIND, global_parameter_name,
        state_name=state_name, action="exists", reason=reason,
        change_summary=change_summary, definition=updated_properties.get(global_parameter_name),
    )
    await db.commit()
    return {
        "global_parameter_name": global_parameter_name,
        "updated": True,
        "reason": reason,
        "change_summary": change_summary,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def list_global_parameter_snapshots(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Lists every named checkpoint saved for this global parameter, oldest first."""
    return {
        "global_parameter_name": global_parameter_name,
        "states": await ck.list_snapshots(db, project, _KIND, global_parameter_name),
    }


async def rollback_global_parameter_definition(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    state_name: str,
    confirm_delete: bool = False,
) -> dict:
    """
    Jumps directly to any named checkpoint, regardless of where it sits in history — nothing
    is ever deleted from local history. For simple one-step undo/redo, use
    back_resource_definition/forward_resource_definition instead.

    If the target checkpoint predates the global parameter's creation, applying it means
    REMOVING it from the factory's global parameters — returns
    {"requires_confirmation": true} instead of removing it; call again with
    confirm_delete=true only after the human has agreed to that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._find_snapshot(db, project, _KIND, global_parameter_name, state_name)
    if target is None:
        return {"error": "state_not_found", "global_parameter_name": global_parameter_name, "state_name": state_name}

    async def delete_fn() -> None:
        properties = await loop.run_in_executor(
            None, lambda: _get_all_properties(client, resource_group, factory_name)
        )
        remaining = {k: v for k, v in properties.items() if k != global_parameter_name}
        if remaining:
            error, _ = await loop.run_in_executor(
                None, lambda: _write_all_properties(client, resource_group, factory_name, remaining)
            )
            if error:
                raise ValueError(error)
        else:
            await loop.run_in_executor(
                None, lambda: client.global_parameters.delete(resource_group, factory_name, _DEFAULT_RESOURCE_NAME)
            )

    async def apply_fn(target_definition: dict) -> None:
        properties = await loop.run_in_executor(
            None, lambda: _get_all_properties(client, resource_group, factory_name)
        )
        merged = {**properties, global_parameter_name: target_definition}
        error, _ = await loop.run_in_executor(
            None, lambda: _write_all_properties(client, resource_group, factory_name, merged)
        )
        if error:
            raise ValueError(error)

    try:
        result = await ck._apply_rollback(
            db, project, _KIND, global_parameter_name, target, reason, delete_fn, apply_fn, confirm_delete
        )
    except ValueError as exc:
        return exc.args[0]
    if result.get("requires_confirmation"):
        result["global_parameter_name"] = global_parameter_name
        return result
    await db.commit()
    result["global_parameter_name"] = global_parameter_name
    return result


async def _global_parameter_navigate(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str, direction: str, confirm_delete: bool,
) -> dict:
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._step_snapshot(db, project, _KIND, global_parameter_name, direction)
    if isinstance(target, dict):
        return {**target, "global_parameter_name": global_parameter_name}

    async def delete_fn() -> None:
        properties = await loop.run_in_executor(
            None, lambda: _get_all_properties(client, resource_group, factory_name)
        )
        remaining = {k: v for k, v in properties.items() if k != global_parameter_name}
        if remaining:
            error, _ = await loop.run_in_executor(
                None, lambda: _write_all_properties(client, resource_group, factory_name, remaining)
            )
            if error:
                raise ValueError(error)
        else:
            await loop.run_in_executor(
                None, lambda: client.global_parameters.delete(resource_group, factory_name, _DEFAULT_RESOURCE_NAME)
            )

    async def apply_fn(definition: dict) -> None:
        properties = await loop.run_in_executor(
            None, lambda: _get_all_properties(client, resource_group, factory_name)
        )
        merged = {**properties, global_parameter_name: definition}
        error, _ = await loop.run_in_executor(
            None, lambda: _write_all_properties(client, resource_group, factory_name, merged)
        )
        if error:
            raise ValueError(error)

    try:
        result = await ck._navigate(
            db, project, _KIND, global_parameter_name, target, delete_fn, apply_fn, confirm_delete
        )
    except ValueError as exc:
        return exc.args[0]
    if not result.get("requires_confirmation"):
        await db.commit()
    result["global_parameter_name"] = global_parameter_name
    result["reason"] = reason
    return result


async def back_global_parameter_definition(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint back through this global parameter's history — see back_pipeline_definition."""
    return await _global_parameter_navigate(
        db, project, global_parameter_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "back", confirm_delete,
    )


async def forward_global_parameter_definition(
    db: AsyncSession, project: str,
    global_parameter_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint forward through this global parameter's history — see forward_pipeline_definition."""
    return await _global_parameter_navigate(
        db, project, global_parameter_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "forward", confirm_delete,
    )
