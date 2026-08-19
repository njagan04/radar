from datetime import UTC, datetime, timedelta

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory.models import RunFilterParameters, TriggerResource

from mcp_servers.adf.tools._shared import (
    _client,
    _reject_if_dropped_fields,
    _reject_if_miscased,
    _to_ist,
)

# Unlike every other resource kind, triggers have no get_trigger_definition_raw /
# get_resource_definition_raw equivalent — get_trigger below reports only name/type/
# runtime_state (Started/Stopped/Disabled), not the full editable definition.


def get_trigger(
    trigger_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
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
    trigger_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    reason: str,
) -> dict:
    """Starts a stopped/disabled trigger."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    poller = client.triggers.begin_start(resource_group, factory_name, trigger_name)
    poller.result()
    trigger = client.triggers.get(resource_group, factory_name, trigger_name)
    runtime_state = (
        trigger.properties.runtime_state if trigger and trigger.properties else None
    )
    return {"name": trigger_name, "reason": reason, "runtime_state": runtime_state}


def stop_trigger(
    trigger_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
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
    runtime_state = (
        trigger.properties.runtime_state if trigger and trigger.properties else None
    )
    return {"name": trigger_name, "reason": reason, "runtime_state": runtime_state}


def get_trigger_run_history(
    trigger_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    days: int = 7,
) -> dict:
    """
    Trigger-run history — distinct from pipeline-run history. Needed for tumbling-window
    and event triggers, where the trigger run (not the pipeline run) is the unit that can
    fail, be rerun, or be cancelled independently of the pipeline it invokes.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(UTC)
    run_filter = RunFilterParameters(
        last_updated_after=now - timedelta(days=days),
        last_updated_before=now,
    )
    runs = client.trigger_runs.query_by_factory(
        resource_group, factory_name, run_filter
    )
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
    trigger_name: str,
    trigger_run_id: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    reason: str,
) -> dict:
    """
    Reruns a specific trigger run. Needed for tumbling-window/event triggers that don't
    go through pipelines.create_run — rerun_pipeline can't retry these.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    client.trigger_runs.rerun(
        resource_group, factory_name, trigger_name, trigger_run_id
    )
    return {
        "trigger_name": trigger_name,
        "trigger_run_id": trigger_run_id,
        "reran": True,
        "reason": reason,
    }


def cancel_trigger_run(
    trigger_name: str,
    trigger_run_id: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    reason: str,
) -> dict:
    """Cancels a specific in-progress trigger run (tumbling-window/event triggers)."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    client.trigger_runs.cancel(
        resource_group, factory_name, trigger_name, trigger_run_id
    )
    return {
        "trigger_name": trigger_name,
        "trigger_run_id": trigger_run_id,
        "cancelled": True,
        "reason": reason,
    }


def list_triggers(
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
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


def create_trigger(
    trigger_name: str,
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
    Creates a brand-new trigger (e.g. a ScheduleTrigger). Fails with an explicit error if a
    trigger with this name already exists — use update_resource_definition to modify an
    existing one instead. Created in a Stopped state, same as ADF Studio's default — call
    start_trigger separately once you've verified it. `definition` should be the flat shape
    (properties.type, typeProperties, pipelines, etc. at the top level) matching what
    get_trigger's raw definition would show.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    try:
        client.triggers.get(resource_group, factory_name, trigger_name)
        return {"error": "trigger_already_exists", "trigger_name": trigger_name}
    except ResourceNotFoundError:
        pass

    error = _reject_if_dropped_fields(
        {"properties": definition}, TriggerResource, "trigger"
    )
    if error:
        return error
    trigger_resource = TriggerResource.deserialize({"properties": definition})
    error = _reject_if_miscased(trigger_resource, "trigger")
    if error:
        return error
    created = client.triggers.create_or_update(
        resource_group, factory_name, trigger_name, trigger_resource
    )

    return {
        "trigger_name": trigger_name,
        "created": True,
        "reason": reason,
        "etag": created.etag,
    }


def update_trigger_definition(
    trigger_name: str,
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
    Overwrites a trigger's full definition (e.g. to correct a wrong schedule/recurrence).
    Does not change the trigger's Started/Stopped runtime state — use start_trigger/
    stop_trigger for that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    error = _reject_if_dropped_fields(
        {"properties": definition}, TriggerResource, "trigger"
    )
    if error:
        return error
    trigger_resource = TriggerResource.deserialize({"properties": definition})
    error = _reject_if_miscased(trigger_resource, "trigger")
    if error:
        return error
    updated = client.triggers.create_or_update(
        resource_group, factory_name, trigger_name, trigger_resource
    )

    return {
        "trigger_name": trigger_name,
        "updated": True,
        "reason": reason,
        "etag": updated.etag,
    }
