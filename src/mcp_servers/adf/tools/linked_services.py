import asyncio

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import LinkedServiceResource
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.adf.tools import _checkpoints as ck
from mcp_servers.adf.tools._shared import _client, _reject_if_dropped_fields, _reject_if_miscased, _to_wire_dict

_KIND = "linked_service"  # matches resource_snapshots' CHECK constraint and _dispatch.py's
# _NAME_KWARG_BY_KIND — NOT claude-desktop's "linkedservice" (no underscore), which was
# only ever a local-disk folder-naming convention there, not a value anything here checks.


def get_linked_service(
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Evidence loop — name and type only (e.g. "AzureSqlDatabase"). Does NOT include the
    actual configured host/port/connection string — those live in typeProperties, which
    this deliberately lightweight tool omits. Use get_resource_definition_raw
    (resource_type="linked_service") for the real connection details.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    svc = client.linked_services.get(resource_group, factory_name, service_name)
    if svc is None:
        return {"name": service_name, "type": "unknown"}
    return {"name": svc.name, "type": svc.properties.type if svc.properties else "unknown"}


def list_linked_services(
    factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Factory-wide linked-service sweep, vs. get_linked_service's single lookup. Useful when
    the failing linked service isn't known by name up front.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    services = client.linked_services.list_by_factory(resource_group, factory_name)
    return {
        "linked_services": [
            {"name": s.name, "type": s.properties.type if s.properties else "unknown"}
            for s in services
        ]
    }


def get_linked_service_definition_raw(
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Full linked-service definition — the actual configured host/port/connection string live
    in typeProperties (e.g. AzureSqlDatabase's typeProperties.connectionString), which
    get_linked_service deliberately omits. The editable structure to feed back into
    update_resource_definition once a fix is decided.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    svc = client.linked_services.get(resource_group, factory_name, service_name)
    return _to_wire_dict(svc)


# --- Checkpoint-enabled tools below — async, see pipelines.py's module comment for why. ---
# Note the one real difference from pipelines: LinkedServiceResource.deserialize() always
# needs the {"properties": ...} wrapper (create_pipeline instead unwraps an optional ARM
# wrapper before deserializing directly) — preserved exactly as claude-desktop's original.

async def create_linked_service(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    state_name: str | None = None,
) -> dict:
    """
    Creates a brand-new linked service. Fails with an explicit error if one with this name
    already exists — use update_resource_definition to modify an existing one instead.
    Records two checkpoints: "before-creation" and one for the just-created content, named
    `state_name` if given (default "created").
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, lambda: client.linked_services.get(resource_group, factory_name, service_name))
        return {"error": "linked_service_already_exists", "service_name": service_name}
    except ResourceNotFoundError:
        pass

    await ck._push_snapshot(
        db, project, _KIND, service_name,
        state_name="before-creation", action="create", reason=reason, change_summary="linked service did not exist",
    )

    error = _reject_if_dropped_fields({"properties": definition}, LinkedServiceResource, "linked service")
    if error:
        return error
    linked_service_resource = LinkedServiceResource.deserialize({"properties": definition})
    error = _reject_if_miscased(linked_service_resource, "linked service")
    if error:
        return error
    created = await loop.run_in_executor(
        None,
        lambda: client.linked_services.create_or_update(resource_group, factory_name, service_name, linked_service_resource),
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, service_name,
        state_name=state_name or "created", action="exists", reason=reason,
        change_summary="linked service created", definition=_to_wire_dict(created),
    )
    await db.commit()
    return {
        "service_name": service_name,
        "created": True,
        "reason": reason,
        "saved_state_name": saved["state_name"],
        "etag": created.etag,
    }


async def update_linked_service_definition(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    change_summary: str,
    state_name: str | None = None,
) -> dict:
    """
    Overwrites a linked service's full definition (e.g. to correct a wrong host/port in a
    `network`/`config` failure). If it has no history yet, its as-found content is captured
    as an "initial" checkpoint first.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    current = await loop.run_in_executor(None, lambda: client.linked_services.get(resource_group, factory_name, service_name))
    await ck._ensure_baseline(db, project, _KIND, service_name, _to_wire_dict(current), reason)

    error = _reject_if_dropped_fields({"properties": definition}, LinkedServiceResource, "linked service")
    if error:
        return error
    linked_service_resource = LinkedServiceResource.deserialize({"properties": definition})
    error = _reject_if_miscased(linked_service_resource, "linked service")
    if error:
        return error
    updated = await loop.run_in_executor(
        None,
        lambda: client.linked_services.create_or_update(resource_group, factory_name, service_name, linked_service_resource),
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, service_name,
        state_name=state_name, action="exists", reason=reason,
        change_summary=change_summary, definition=_to_wire_dict(updated),
    )
    await db.commit()
    return {
        "service_name": service_name,
        "updated": True,
        "reason": reason,
        "change_summary": change_summary,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def list_linked_service_snapshots(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Lists every named checkpoint saved for this linked service, oldest first."""
    return {"service_name": service_name, "states": await ck.list_snapshots(db, project, _KIND, service_name)}


async def rollback_linked_service_definition(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
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

    If the target checkpoint predates this linked service's creation, applying it means
    DELETING it — returns {"requires_confirmation": true} instead of deleting; call again
    with confirm_delete=true only after the human has agreed to that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._find_snapshot(db, project, _KIND, service_name, state_name)
    if target is None:
        return {"error": "state_not_found", "service_name": service_name, "state_name": state_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.linked_services.delete(resource_group, factory_name, service_name))

    async def apply_fn(target_definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": target_definition}, LinkedServiceResource, "linked service")
        if error:
            raise ValueError(error)
        linked_service_resource = LinkedServiceResource.deserialize({"properties": target_definition})
        error = _reject_if_miscased(linked_service_resource, "linked service")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.linked_services.create_or_update(resource_group, factory_name, service_name, linked_service_resource),
        )

    try:
        result = await ck._apply_rollback(db, project, _KIND, service_name, target, reason, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if result.get("requires_confirmation"):
        result["service_name"] = service_name
        return result
    await db.commit()
    result["service_name"] = service_name
    return result


async def _linked_service_navigate(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str, direction: str, confirm_delete: bool,
) -> dict:
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._step_snapshot(db, project, _KIND, service_name, direction)
    if isinstance(target, dict):
        return {**target, "service_name": service_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.linked_services.delete(resource_group, factory_name, service_name))

    async def apply_fn(definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": definition}, LinkedServiceResource, "linked service")
        if error:
            raise ValueError(error)
        linked_service_resource = LinkedServiceResource.deserialize({"properties": definition})
        error = _reject_if_miscased(linked_service_resource, "linked service")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.linked_services.create_or_update(resource_group, factory_name, service_name, linked_service_resource),
        )

    try:
        result = await ck._navigate(db, project, _KIND, service_name, target, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if not result.get("requires_confirmation"):
        await db.commit()
    result["service_name"] = service_name
    result["reason"] = reason
    return result


async def back_linked_service_definition(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint back through this linked service's history — see back_pipeline_definition."""
    return await _linked_service_navigate(
        db, project, service_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "back", confirm_delete,
    )


async def forward_linked_service_definition(
    db: AsyncSession, project: str,
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint forward through this linked service's history — see forward_pipeline_definition."""
    return await _linked_service_navigate(
        db, project, service_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "forward", confirm_delete,
    )
