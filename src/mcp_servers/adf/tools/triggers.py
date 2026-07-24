import asyncio
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import RunFilterParameters, TriggerResource
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.adf.tools import _checkpoints as ck
from mcp_servers.adf.tools._shared import _client, _reject_if_dropped_fields, _reject_if_miscased, _to_ist, _to_wire_dict

# Kind-naming note (same trap checked proactively for data_flow/global_parameter): claude-
# desktop's original uses bare "trigger" for its disk-folder name, which happens to already
# match the Postgres CHECK constraint's "trigger" exactly - no underscore mismatch here,
# verified against models.py before writing this file regardless, since two of the last three
# kinds got this wrong.
_KIND = "trigger"


# --- Ported from claude-desktop/mcp_adf/tools/triggers.py, read-only / action tools below
# (no checkpoint involvement, stay synchronous exactly like the source). Note: unlike every
# other kind, triggers deliberately has NO get_trigger_definition_raw / get_resource_
# definition_raw equivalent - get_trigger below reports only name/type/runtime_state (whether
# it's Started/Stopped/Disabled), not the full editable definition. This is intentional in
# the original (see mcp_servers/adf/tools/_dispatch.py's docstring) and carried over as-is:
# "trigger" is absent from _GET_DEFINITION_RAW_BY_KIND even though it's present everywhere
# else. ---

def get_trigger(
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Read-only. Reports whether a trigger is Started/Stopped/Disabled."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    trigger = client.triggers.get(resource_group, factory_name, trigger_name)
    if trigger is None or trigger.properties is None:
        return {"name": trigger_name, "runtime_state": "unknown"}
    return {
        "name": trigger_name,
        "type": trigger.properties.type,
        "runtime_state": trigger.properties.runtime_state,
    }


def start_trigger(
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
) -> dict:
    """Starts a stopped/disabled trigger."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    poller = client.triggers.begin_start(resource_group, factory_name, trigger_name)
    poller.result()
    trigger = client.triggers.get(resource_group, factory_name, trigger_name)
    runtime_state = trigger.properties.runtime_state if trigger and trigger.properties else None
    return {"name": trigger_name, "reason": reason, "runtime_state": runtime_state}


def stop_trigger(
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
) -> dict:
    """
    Stops a running trigger. Inverse of start_trigger — needed if a trigger is misfiring
    after a fix and needs to be paused before it does more damage.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    poller = client.triggers.begin_stop(resource_group, factory_name, trigger_name)
    poller.result()
    trigger = client.triggers.get(resource_group, factory_name, trigger_name)
    runtime_state = trigger.properties.runtime_state if trigger and trigger.properties else None
    return {"name": trigger_name, "reason": reason, "runtime_state": runtime_state}


def get_trigger_run_history(
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    days: int = 7,
) -> dict:
    """
    Trigger-run history — distinct from pipeline-run history. Needed for tumbling-window
    and event triggers, where the trigger run (not the pipeline run) is the unit that can
    fail, be rerun, or be cancelled independently of the pipeline it invokes.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(timezone.utc)
    run_filter = RunFilterParameters(
        last_updated_after=now - timedelta(days=days),
        last_updated_before=now,
    )
    runs = client.trigger_runs.query_by_factory(resource_group, factory_name, run_filter)
    return {
        "runs": [
            {
                "trigger_run_id": r.trigger_run_id,
                "trigger_name": r.trigger_name,
                "status": r.status,
                "message": r.message,
                "timestamp": str(r.trigger_run_timestamp),
                "timestamp_ist": _to_ist(r.trigger_run_timestamp),
            }
            for r in runs.value
            if r.trigger_name == trigger_name
        ]
    }


def rerun_trigger_run(
    trigger_name: str, trigger_run_id: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
) -> dict:
    """
    Reruns a specific trigger run. Needed for tumbling-window/event triggers that don't
    go through pipelines.create_run — rerun_pipeline can't retry these.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    client.trigger_runs.rerun(resource_group, factory_name, trigger_name, trigger_run_id)
    return {"trigger_name": trigger_name, "trigger_run_id": trigger_run_id, "reran": True, "reason": reason}


def cancel_trigger_run(
    trigger_name: str, trigger_run_id: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
) -> dict:
    """Cancels a specific in-progress trigger run (tumbling-window/event triggers)."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    client.trigger_runs.cancel(resource_group, factory_name, trigger_name, trigger_run_id)
    return {"trigger_name": trigger_name, "trigger_run_id": trigger_run_id, "cancelled": True, "reason": reason}


def list_triggers(
    factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Factory-wide trigger sweep — every trigger's name and runtime state in one call."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    triggers = client.triggers.list_by_factory(resource_group, factory_name)
    return {
        "triggers": [
            {
                "name": t.name,
                "type": getattr(t.properties, "type", None),
                "runtime_state": getattr(t.properties, "runtime_state", None),
            }
            for t in triggers
        ]
    }


# --- Checkpoint-enabled tools below — async, see pipelines.py's module comment for why.
# Trigger's wrapping pattern matches linked_service's exactly: always wrap the given
# definition in {"properties": ...} before deserializing, no optional ARM-export-shape
# unwrap step (unlike pipeline/dataset/data_flow, which all accept an ARM-wrapped input too).
# Also note: create_trigger creates in a Stopped state, same as ADF Studio's default -
# start_trigger must be called separately once verified. ---

async def create_trigger(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    state_name: str | None = None,
) -> dict:
    """
    Creates a brand-new trigger (e.g. a ScheduleTrigger). Fails with an explicit error if a
    trigger with this name already exists — use update_resource_definition to modify an
    existing one instead. Created in a Stopped state, same as ADF Studio's default — call
    start_trigger separately once you've verified it. Records two checkpoints: "before-
    creation" and one for the just-created content, named `state_name` if given (default
    "created"). `definition` should be the flat shape (properties.type, typeProperties,
    pipelines, etc. at the top level) matching what get_trigger's raw definition would show.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, lambda: client.triggers.get(resource_group, factory_name, trigger_name))
        return {"error": "trigger_already_exists", "trigger_name": trigger_name}
    except ResourceNotFoundError:
        pass

    await ck._push_snapshot(
        db, project, _KIND, trigger_name,
        state_name="before-creation", action="create", reason=reason, change_summary="trigger did not exist",
    )

    error = _reject_if_dropped_fields({"properties": definition}, TriggerResource, "trigger")
    if error:
        return error
    trigger_resource = TriggerResource.deserialize({"properties": definition})
    error = _reject_if_miscased(trigger_resource, "trigger")
    if error:
        return error
    created = await loop.run_in_executor(
        None, lambda: client.triggers.create_or_update(resource_group, factory_name, trigger_name, trigger_resource)
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, trigger_name,
        state_name=state_name or "created", action="exists", reason=reason,
        change_summary="trigger created", definition=_to_wire_dict(created),
    )
    await db.commit()
    return {
        "trigger_name": trigger_name,
        "created": True,
        "reason": reason,
        "saved_state_name": saved["state_name"],
        "etag": created.etag,
    }


async def update_trigger_definition(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    change_summary: str,
    state_name: str | None = None,
) -> dict:
    """
    Overwrites a trigger's full definition (e.g. to correct a wrong schedule/recurrence). If
    this trigger has no history yet, its as-found content is captured as an "initial"
    checkpoint first. Does not change the trigger's Started/Stopped runtime state — use
    start_trigger/stop_trigger for that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    current = await loop.run_in_executor(None, lambda: client.triggers.get(resource_group, factory_name, trigger_name))
    await ck._ensure_baseline(db, project, _KIND, trigger_name, _to_wire_dict(current), reason)

    error = _reject_if_dropped_fields({"properties": definition}, TriggerResource, "trigger")
    if error:
        return error
    trigger_resource = TriggerResource.deserialize({"properties": definition})
    error = _reject_if_miscased(trigger_resource, "trigger")
    if error:
        return error
    updated = await loop.run_in_executor(
        None, lambda: client.triggers.create_or_update(resource_group, factory_name, trigger_name, trigger_resource)
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, trigger_name,
        state_name=state_name, action="exists", reason=reason,
        change_summary=change_summary, definition=_to_wire_dict(updated),
    )
    await db.commit()
    return {
        "trigger_name": trigger_name,
        "updated": True,
        "reason": reason,
        "change_summary": change_summary,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def list_trigger_snapshots(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Lists every named checkpoint saved for this trigger, oldest first."""
    return {"trigger_name": trigger_name, "states": await ck.list_snapshots(db, project, _KIND, trigger_name)}


async def rollback_trigger_definition(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    state_name: str,
    confirm_delete: bool = False,
) -> dict:
    """
    Jumps directly to any named checkpoint, regardless of where it sits in history — nothing
    is ever deleted. For simple one-step undo/redo, use back_resource_definition/
    forward_resource_definition instead.

    If the target checkpoint predates the trigger's creation, applying it means DELETING it
    — returns {"requires_confirmation": true} instead of deleting; call again with
    confirm_delete=true only after the human has agreed to that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._find_snapshot(db, project, _KIND, trigger_name, state_name)
    if target is None:
        return {"error": "state_not_found", "trigger_name": trigger_name, "state_name": state_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.triggers.delete(resource_group, factory_name, trigger_name))

    async def apply_fn(target_definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": target_definition}, TriggerResource, "trigger")
        if error:
            raise ValueError(error)
        trigger_resource = TriggerResource.deserialize({"properties": target_definition})
        error = _reject_if_miscased(trigger_resource, "trigger")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.triggers.create_or_update(resource_group, factory_name, trigger_name, trigger_resource),
        )

    try:
        result = await ck._apply_rollback(db, project, _KIND, trigger_name, target, reason, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if result.get("requires_confirmation"):
        result["trigger_name"] = trigger_name
        return result
    await db.commit()
    result["trigger_name"] = trigger_name
    return result


async def _trigger_navigate(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str, direction: str, confirm_delete: bool,
) -> dict:
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._step_snapshot(db, project, _KIND, trigger_name, direction)
    if isinstance(target, dict):
        return {**target, "trigger_name": trigger_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.triggers.delete(resource_group, factory_name, trigger_name))

    async def apply_fn(definition: dict) -> None:
        error = _reject_if_dropped_fields({"properties": definition}, TriggerResource, "trigger")
        if error:
            raise ValueError(error)
        trigger_resource = TriggerResource.deserialize({"properties": definition})
        error = _reject_if_miscased(trigger_resource, "trigger")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.triggers.create_or_update(resource_group, factory_name, trigger_name, trigger_resource),
        )

    try:
        result = await ck._navigate(db, project, _KIND, trigger_name, target, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if not result.get("requires_confirmation"):
        await db.commit()
    result["trigger_name"] = trigger_name
    result["reason"] = reason
    return result


async def back_trigger_definition(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint back through this trigger's history — see back_pipeline_definition."""
    return await _trigger_navigate(
        db, project, trigger_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "back", confirm_delete,
    )


async def forward_trigger_definition(
    db: AsyncSession, project: str,
    trigger_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """Steps one checkpoint forward through this trigger's history — see forward_pipeline_definition."""
    return await _trigger_navigate(
        db, project, trigger_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "forward", confirm_delete,
    )
