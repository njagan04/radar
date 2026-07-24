import asyncio

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DataFlowResource
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.adf.tools import _checkpoints as ck
from mcp_servers.adf.tools._shared import _client, _reject_if_dropped_fields, _reject_if_miscased, _to_wire_dict

# claude-desktop's original data_flows.py uses the bare string "dataflow" (no underscore) as
# its checkpoint kind — that was fine on local-disk storage, but Postgres's
# ck_resource_snapshots_kind CHECK constraint (src/db/models.py) requires "data_flow" (with
# underscore), same as the linked_service kind-naming bug found earlier in this port.
# Verified against models.py before writing this file.
_KIND = "data_flow"


def list_data_flows(
    factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
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


def get_data_flow_definition_raw(
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Full Mapping Data Flow definition (sources, sinks, transformation script). Pipeline
    definition tools only show that an activity references a data flow by name — this is
    the only way to see (and diagnose `schema_drift`/`business_logic` failures inside) the
    transformation graph itself. The editable structure to feed back into
    update_resource_definition.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    data_flow = client.data_flows.get(resource_group, factory_name, data_flow_name)
    return _to_wire_dict(data_flow)


# --- Checkpoint-enabled tools below — async, see pipelines.py's module comment for why.
# Same third wrapping pattern as datasets.py: create_data_flow unwraps an optional ARM
# export wrapper (definition.get("properties", definition)) then RE-wraps in
# {"properties": ...} before deserializing, since DataFlowResource needs that wrapper the
# same way LinkedServiceResource/DatasetResource do. update/rollback/back/forward always
# just wrap the given definition directly (no unwrap step). ---

async def create_data_flow(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    state_name: str | None = None,
) -> dict:
    """
    Creates a brand-new data flow. Fails with an explicit error if a data flow with this
    name already exists — use update_resource_definition to modify an existing one instead.
    Records two checkpoints: "before-creation" and one for the just-created content, named
    `state_name` if given (default "created").

    `definition` accepts either the flat shape get_data_flow_definition_raw uses, or the
    ARM/Studio-export shape ({"name": ..., "properties": {...}}) — if a "properties" key is
    present, its contents are used and the wrapper is discarded. `data_flow_name` always
    determines the actual name created.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(
            None, lambda: client.data_flows.get(resource_group, factory_name, data_flow_name)
        )
        return {"error": "data_flow_already_exists", "data_flow_name": data_flow_name}
    except ResourceNotFoundError:
        pass

    await ck._push_snapshot(
        db, project, _KIND, data_flow_name,
        state_name="before-creation", action="create", reason=reason, change_summary="data flow did not exist",
    )

    properties = definition.get("properties", definition)
    error = _reject_if_dropped_fields({"properties": properties}, DataFlowResource, "data flow")
    if error:
        return error
    data_flow_resource = DataFlowResource.deserialize({"properties": properties})
    error = _reject_if_miscased(data_flow_resource, "data flow")
    if error:
        return error
    created = await loop.run_in_executor(
        None,
        lambda: client.data_flows.create_or_update(resource_group, factory_name, data_flow_name, data_flow_resource),
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, data_flow_name,
        state_name=state_name or "created", action="exists", reason=reason,
        change_summary="data flow created", definition=_to_wire_dict(created),
    )
    await db.commit()
    return {
        "data_flow_name": data_flow_name,
        "created": True,
        "reason": reason,
        "saved_state_name": saved["state_name"],
        "etag": created.etag,
    }


async def update_data_flow_definition(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    change_summary: str,
    state_name: str | None = None,
) -> dict:
    """
    Overwrites a data flow's full definition. If this data flow has no history yet, its
    as-found content is captured as an "initial" checkpoint first.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    current = await loop.run_in_executor(
        None, lambda: client.data_flows.get(resource_group, factory_name, data_flow_name)
    )
    await ck._ensure_baseline(db, project, _KIND, data_flow_name, _to_wire_dict(current), reason)

    error = _reject_if_dropped_fields({"properties": definition}, DataFlowResource, "data flow")
    if error:
        return error
    data_flow_resource = DataFlowResource.deserialize({"properties": definition})
    error = _reject_if_miscased(data_flow_resource, "data flow")
    if error:
        return error
    updated = await loop.run_in_executor(
        None,
        lambda: client.data_flows.create_or_update(resource_group, factory_name, data_flow_name, data_flow_resource),
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, data_flow_name,
        state_name=state_name, action="exists", reason=reason,
        change_summary=change_summary, definition=_to_wire_dict(updated),
    )
    await db.commit()
    return {
        "data_flow_name": data_flow_name,
        "updated": True,
        "reason": reason,
        "change_summary": change_summary,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def list_data_flow_snapshots(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Lists every named checkpoint saved for this data flow, oldest first."""
    return {"data_flow_name": data_flow_name, "states": await ck.list_snapshots(db, project, _KIND, data_flow_name)}


async def rollback_data_flow_definition(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    state_name: str,
    confirm_delete: bool = False,
) -> dict:
    """
    Jumps directly to any named checkpoint, regardless of where it sits in history — nothing
    is ever deleted outright. For simple one-step undo/redo, use back_resource_definition/
    forward_resource_definition instead.

    If the target checkpoint predates the data flow's creation, applying it means DELETING
    it — returns {"requires_confirmation": true} instead of deleting; call again with
    confirm_delete=true only after the human has agreed to that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._find_snapshot(db, project, _KIND, data_flow_name, state_name)
    if target is None:
        return {"error": "state_not_found", "data_flow_name": data_flow_name, "state_name": state_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(
            None, lambda: client.data_flows.delete(resource_group, factory_name, data_flow_name)
        )

    async def apply_fn(target_definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": target_definition}, DataFlowResource, "data flow")
        if error:
            raise ValueError(error)
        data_flow_resource = DataFlowResource.deserialize({"properties": target_definition})
        error = _reject_if_miscased(data_flow_resource, "data flow")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.data_flows.create_or_update(
                resource_group, factory_name, data_flow_name, data_flow_resource
            ),
        )

    try:
        result = await ck._apply_rollback(
            db, project, _KIND, data_flow_name, target, reason, delete_fn, apply_fn, confirm_delete
        )
    except ValueError as exc:
        return exc.args[0]
    if result.get("requires_confirmation"):
        result["data_flow_name"] = data_flow_name
        return result
    await db.commit()
    result["data_flow_name"] = data_flow_name
    return result


async def _data_flow_navigate(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str, direction: str, confirm_delete: bool,
) -> dict:
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._step_snapshot(db, project, _KIND, data_flow_name, direction)
    if isinstance(target, dict):
        return {**target, "data_flow_name": data_flow_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(
            None, lambda: client.data_flows.delete(resource_group, factory_name, data_flow_name)
        )

    async def apply_fn(definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": definition}, DataFlowResource, "data flow")
        if error:
            raise ValueError(error)
        data_flow_resource = DataFlowResource.deserialize({"properties": definition})
        error = _reject_if_miscased(data_flow_resource, "data flow")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.data_flows.create_or_update(
                resource_group, factory_name, data_flow_name, data_flow_resource
            ),
        )

    try:
        result = await ck._navigate(db, project, _KIND, data_flow_name, target, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if not result.get("requires_confirmation"):
        await db.commit()
    result["data_flow_name"] = data_flow_name
    result["reason"] = reason
    return result


async def back_data_flow_definition(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint back through this data flow's history — see back_pipeline_definition."""
    return await _data_flow_navigate(
        db, project, data_flow_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "back", confirm_delete,
    )


async def forward_data_flow_definition(
    db: AsyncSession, project: str,
    data_flow_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint forward through this data flow's history — see forward_pipeline_definition."""
    return await _data_flow_navigate(
        db, project, data_flow_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "forward", confirm_delete,
    )
