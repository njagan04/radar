import asyncio

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import DatasetResource
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.adf.tools import _checkpoints as ck
from mcp_servers.adf.tools._shared import _client, _reject_if_dropped_fields, _reject_if_miscased, _to_wire_dict

_KIND = "dataset"


def list_datasets(
    factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
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
                    if d.properties and d.properties.linked_service_name else None
                ),
            }
            for d in datasets
        ]
    }


def get_dataset_definition_raw(
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Full dataset definition (schema, structure, linked service reference, parameters) — the
    evidence needed to diagnose `schema_drift` (source/sink no longer matches the dataset's
    declared schema). The editable structure to feed back into update_resource_definition.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    dataset = client.datasets.get(resource_group, factory_name, dataset_name)
    return _to_wire_dict(dataset)


# --- Checkpoint-enabled tools below — async, see pipelines.py's module comment for why.
# Note the shape create_dataset accepts: like create_pipeline, it unwraps an optional ARM
# export wrapper first (definition.get("properties", definition)) — but unlike
# create_pipeline, it then RE-wraps in {"properties": ...} before deserializing, since
# DatasetResource needs that wrapper the same way LinkedServiceResource does. update/
# rollback/back/forward always just wrap the given definition directly (no unwrap step —
# they operate on get_dataset_definition_raw's already-flat output). ---

async def create_dataset(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    state_name: str | None = None,
) -> dict:
    """
    Creates a brand-new dataset. Fails with an explicit error if a dataset with this name
    already exists — use update_resource_definition to modify an existing one instead.
    Records two checkpoints: "before-creation" and one for the just-created content, named
    `state_name` if given (default "created").

    `definition` accepts either the flat shape get_dataset_definition_raw uses, or the ARM/
    Studio-export shape ({"name": ..., "properties": {...}}) — if a "properties" key is
    present, its contents are used and the wrapper is discarded. `dataset_name` always
    determines the actual name created.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, lambda: client.datasets.get(resource_group, factory_name, dataset_name))
        return {"error": "dataset_already_exists", "dataset_name": dataset_name}
    except ResourceNotFoundError:
        pass

    await ck._push_snapshot(
        db, project, _KIND, dataset_name,
        state_name="before-creation", action="create", reason=reason, change_summary="dataset did not exist",
    )

    properties = definition.get("properties", definition)
    error = _reject_if_dropped_fields({"properties": properties}, DatasetResource, "dataset")
    if error:
        return error
    dataset_resource = DatasetResource.deserialize({"properties": properties})
    error = _reject_if_miscased(dataset_resource, "dataset")
    if error:
        return error
    created = await loop.run_in_executor(
        None, lambda: client.datasets.create_or_update(resource_group, factory_name, dataset_name, dataset_resource)
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, dataset_name,
        state_name=state_name or "created", action="exists", reason=reason,
        change_summary="dataset created", definition=_to_wire_dict(created),
    )
    await db.commit()
    return {
        "dataset_name": dataset_name,
        "created": True,
        "reason": reason,
        "saved_state_name": saved["state_name"],
        "etag": created.etag,
    }


async def update_dataset_definition(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    change_summary: str,
    state_name: str | None = None,
) -> dict:
    """
    Overwrites a dataset's full definition (e.g. to correct a drifted schema). If this
    dataset has no history yet, its as-found content is captured as an "initial" checkpoint
    first.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    current = await loop.run_in_executor(None, lambda: client.datasets.get(resource_group, factory_name, dataset_name))
    await ck._ensure_baseline(db, project, _KIND, dataset_name, _to_wire_dict(current), reason)

    error = _reject_if_dropped_fields({"properties": definition}, DatasetResource, "dataset")
    if error:
        return error
    dataset_resource = DatasetResource.deserialize({"properties": definition})
    error = _reject_if_miscased(dataset_resource, "dataset")
    if error:
        return error
    updated = await loop.run_in_executor(
        None, lambda: client.datasets.create_or_update(resource_group, factory_name, dataset_name, dataset_resource)
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, dataset_name,
        state_name=state_name, action="exists", reason=reason,
        change_summary=change_summary, definition=_to_wire_dict(updated),
    )
    await db.commit()
    return {
        "dataset_name": dataset_name,
        "updated": True,
        "reason": reason,
        "change_summary": change_summary,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def list_dataset_snapshots(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Lists every named checkpoint saved for this dataset, oldest first."""
    return {"dataset_name": dataset_name, "states": await ck.list_snapshots(db, project, _KIND, dataset_name)}


async def rollback_dataset_definition(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
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

    If the target checkpoint predates the dataset's creation, applying it means DELETING it
    — returns {"requires_confirmation": true} instead of deleting; call again with
    confirm_delete=true only after the human has agreed to that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._find_snapshot(db, project, _KIND, dataset_name, state_name)
    if target is None:
        return {"error": "state_not_found", "dataset_name": dataset_name, "state_name": state_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.datasets.delete(resource_group, factory_name, dataset_name))

    async def apply_fn(target_definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": target_definition}, DatasetResource, "dataset")
        if error:
            raise ValueError(error)
        dataset_resource = DatasetResource.deserialize({"properties": target_definition})
        error = _reject_if_miscased(dataset_resource, "dataset")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.datasets.create_or_update(resource_group, factory_name, dataset_name, dataset_resource),
        )

    try:
        result = await ck._apply_rollback(db, project, _KIND, dataset_name, target, reason, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if result.get("requires_confirmation"):
        result["dataset_name"] = dataset_name
        return result
    await db.commit()
    result["dataset_name"] = dataset_name
    return result


async def _dataset_navigate(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str, direction: str, confirm_delete: bool,
) -> dict:
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._step_snapshot(db, project, _KIND, dataset_name, direction)
    if isinstance(target, dict):
        return {**target, "dataset_name": dataset_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.datasets.delete(resource_group, factory_name, dataset_name))

    async def apply_fn(definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": definition}, DatasetResource, "dataset")
        if error:
            raise ValueError(error)
        dataset_resource = DatasetResource.deserialize({"properties": definition})
        error = _reject_if_miscased(dataset_resource, "dataset")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.datasets.create_or_update(resource_group, factory_name, dataset_name, dataset_resource),
        )

    try:
        result = await ck._navigate(db, project, _KIND, dataset_name, target, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if not result.get("requires_confirmation"):
        await db.commit()
    result["dataset_name"] = dataset_name
    result["reason"] = reason
    return result


async def back_dataset_definition(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint back through this dataset's history — see back_pipeline_definition."""
    return await _dataset_navigate(
        db, project, dataset_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "back", confirm_delete,
    )


async def forward_dataset_definition(
    db: AsyncSession, project: str,
    dataset_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint forward through this dataset's history — see forward_pipeline_definition."""
    return await _dataset_navigate(
        db, project, dataset_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "forward", confirm_delete,
    )
