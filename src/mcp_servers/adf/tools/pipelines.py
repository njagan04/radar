import asyncio
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.datafactory import DataFactoryManagementClient
from azure.mgmt.datafactory.models import PipelineResource, RunFilterParameters, RunQueryFilter
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.adf.tools import _checkpoints as ck
from mcp_servers.adf.tools._shared import _client, _reject_if_dropped_fields, _reject_if_miscased, _to_ist, _to_wire_dict

_KIND = "pipeline"


def _get_failed_activities_for_run(
    client: DataFactoryManagementClient,
    resource_group: str,
    factory_name: str,
    run_id: str,
    window_start: datetime,
    window_end: datetime,
) -> list:
    """Fetch failed activity runs for a known run_id."""
    activity_filter = RunFilterParameters(
        last_updated_after=window_start,
        last_updated_before=window_end,
    )
    activities = client.activity_runs.query_by_pipeline_run(
        resource_group, factory_name, run_id, activity_filter
    )
    return [a for a in activities.value if a.status == "Failed"]


def _follow_single_chain(
    client: DataFactoryManagementClient,
    resource_group: str,
    factory_name: str,
    root_step: dict,
    window_start: datetime,
    window_end: datetime,
    max_depth: int,
) -> dict:
    """Follow one ExecutePipeline chain from a known starting step to its leaf failure."""
    execution_path = [root_step]
    current_run_id = root_step.pop("_child_run_id", None)
    current_pipeline = root_step.pop("_child_pipeline", "unknown")

    if not current_run_id:
        return {"execution_path": execution_path, "leaf": _make_leaf(root_step)}

    for _ in range(max_depth):
        failed = _get_failed_activities_for_run(
            client, resource_group, factory_name, current_run_id, window_start, window_end
        )
        if not failed:
            break
        activity = failed[0]
        step = {
            "pipeline_name": current_pipeline,
            "run_id": current_run_id,
            "activity_name": activity.activity_name,
            "activity_type": activity.activity_type,
            "error": activity.error,
        }
        execution_path.append(step)
        if activity.activity_type != "ExecutePipeline":
            break
        child_output = activity.output or {}
        child_run_id = child_output.get("pipelineRunId")
        child_pipeline = child_output.get("pipelineName", "unknown")
        if not child_run_id:
            break
        current_run_id = child_run_id
        current_pipeline = child_pipeline

    leaf_step = execution_path[-1]
    return {"execution_path": execution_path, "leaf": _make_leaf(leaf_step)}


def _make_leaf(step: dict) -> dict:
    err = step.get("error") or {}
    return {
        "pipeline_name": step["pipeline_name"],
        "run_id": step["run_id"],
        "activity_name": step["activity_name"],
        "activity_type": step["activity_type"],
        "error_code": err.get("errorCode") if isinstance(err, dict) else getattr(err, "error_code", None),
        "message": err.get("message") if isinstance(err, dict) else getattr(err, "message", None),
    }


def _resolve_nested_failure(
    client: DataFactoryManagementClient,
    resource_group: str,
    factory_name: str,
    run_id: str,
    pipeline_name: str,
    window_start: datetime,
    window_end: datetime,
    max_depth: int = 5,
) -> dict:
    """
    Resolve ALL failed activity branches from a given run_id. Handles parallel activity
    failures by following each failed branch independently (fan-out support).

    Returns:
      - execution_path / depth / leaf  → primary branch (first failed activity)
      - failed_branches                → list of {execution_path, leaf} for every failed branch
    """
    failed_activities = _get_failed_activities_for_run(
        client, resource_group, factory_name, run_id, window_start, window_end
    )
    if not failed_activities:
        return {"run_id": run_id, "execution_path": [], "leaf": None, "failed_branches": []}

    branches: list[dict] = []
    for activity in failed_activities:
        step = {
            "pipeline_name": pipeline_name,
            "run_id": run_id,
            "activity_name": activity.activity_name,
            "activity_type": activity.activity_type,
            "error": activity.error,
        }
        if activity.activity_type == "ExecutePipeline":
            output = activity.output or {}
            child_run_id = output.get("pipelineRunId")
            child_pipeline = output.get("pipelineName", "unknown")
            step["_child_run_id"] = child_run_id
            step["_child_pipeline"] = child_pipeline
            branch = _follow_single_chain(
                client, resource_group, factory_name,
                step, window_start, window_end, max_depth - 1,
            )
        else:
            branch = {"execution_path": [step], "leaf": _make_leaf(step)}
        branch["depth"] = len(branch["execution_path"])
        branches.append(branch)

    if not branches:
        return {"run_id": run_id, "execution_path": [], "leaf": None, "failed_branches": []}

    primary = branches[0]
    return {
        "run_id": run_id,
        "execution_path": primary["execution_path"],
        "depth": primary["depth"],
        "leaf": primary["leaf"],
        "failed_branches": branches,
    }


def get_activity_run_error(
    pipeline_name: str, factory_name: str, event_timestamp: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Resolves run_id from pipeline_name + event_timestamp, then recursively follows
    any ExecutePipeline activity chains until the leaf failed activity is found.

    Example for Master → Pipeline B → Pipeline C → Copy Activity (Failed):
    Returns the full execution path so the classifier and investigator see the real
    root cause, not just "Execute Pipeline activity failed."
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)

    ts = datetime.fromisoformat(event_timestamp).replace(tzinfo=timezone.utc)
    window_start = ts - timedelta(minutes=5)
    window_end = ts + timedelta(minutes=30)

    run_filter = RunFilterParameters(
        last_updated_after=window_start,
        last_updated_before=window_end,
        filters=[RunQueryFilter(operand="PipelineName", operator="Equals", values=[pipeline_name])],
    )
    runs = client.pipeline_runs.query_by_factory(resource_group, factory_name, run_filter)
    failed_runs = [r for r in runs.value if r.status == "Failed"]
    if not failed_runs:
        return {"error": "no_failed_run_found"}

    master_run_id: str | None = failed_runs[0].run_id
    if not master_run_id:
        return {"error": "run_id_unavailable"}
    return _resolve_nested_failure(
        client, resource_group, factory_name, master_run_id, pipeline_name,
        window_start, window_end,
    )


def get_pipeline_run_status(
    run_id: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Freshness check before executing an approved rerun."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    run = client.pipeline_runs.get(resource_group, factory_name, run_id)
    return {"run_id": run_id, "status": run.status, "message": run.message}


def get_pipeline_run_history(
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    days: int = 7,
) -> dict:
    """Evidence loop — recent run patterns for a pipeline."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(timezone.utc)
    run_filter = RunFilterParameters(
        last_updated_after=now - timedelta(days=days),
        last_updated_before=now,
        filters=[RunQueryFilter(operand="PipelineName", operator="Equals", values=[pipeline_name])],
    )
    runs = client.pipeline_runs.query_by_factory(resource_group, factory_name, run_filter)
    return {
        "runs": [
            {"run_id": r.run_id, "status": r.status, "start": str(r.run_start), "end": str(r.run_end)}
            for r in runs.value
        ]
    }


def get_pipeline_definition(
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Evidence loop — pipeline structure with activity types and ExecutePipeline references."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    pipeline = client.pipelines.get(resource_group, factory_name, pipeline_name)
    if pipeline is None:
        return {"name": pipeline_name, "activities": []}
    activities = []
    for a in (pipeline.activities or []):
        activity_type = getattr(a, "type", None) or getattr(a, "activity_type", "unknown")
        entry: dict = {"name": a.name, "type": activity_type}
        if activity_type == "ExecutePipeline":
            try:
                props = getattr(a, "type_properties", None)
                pipeline_ref = getattr(getattr(props, "pipeline", None), "reference_name", None)
                if pipeline_ref:
                    entry["references_pipeline"] = pipeline_ref
            except Exception:
                pass
        activities.append(entry)
    return {"name": pipeline.name, "activities": activities}


def get_activity_run_history(
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    days: int = 7,
) -> dict:
    """
    Returns an aggregated summary of which activities have failed in recent runs of a pipeline.

    Instead of returning raw per-run activity data (expensive in tokens), this aggregates across
    up to 5 recent failed runs and returns per-activity failure counts + most recent error code.
    Useful for identifying recurring activity-level failures without high token cost.

    Returns:
      {
        "pipeline_name": ...,
        "runs_checked": N,
        "failed_activity_summary": [
          {"activity_name": "CopyToSilver", "failure_count": 3,
           "last_error_code": "UserErrorInvalidCredentials", "last_failed_at": "..."},
          ...
        ]
      }
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    window_end = now

    run_filter = RunFilterParameters(
        last_updated_after=window_start,
        last_updated_before=window_end,
        filters=[RunQueryFilter(operand="PipelineName", operator="Equals", values=[pipeline_name])],
    )
    runs = client.pipeline_runs.query_by_factory(resource_group, factory_name, run_filter)
    failed_runs = [r for r in runs.value if r.status == "Failed"][:5]  # cap at 5 most recent

    # Aggregate: activity_name → {failure_count, last_error_code, last_failed_at}
    aggregated: dict[str, dict] = {}
    for run in failed_runs:
        if not run.run_id:
            continue
        try:
            failed_activities = _get_failed_activities_for_run(
                client, resource_group, factory_name, run.run_id, window_start, window_end
            )
        except Exception:
            continue
        for a in failed_activities:
            name = a.activity_name or "unknown"
            err = a.error or {}
            error_code = err.get("errorCode") if isinstance(err, dict) else getattr(err, "error_code", None)
            failed_at = str(a.activity_run_end or a.activity_run_start or "")
            if name not in aggregated:
                aggregated[name] = {"failure_count": 0, "last_error_code": None, "last_failed_at": ""}
            aggregated[name]["failure_count"] += 1
            if failed_at >= aggregated[name]["last_failed_at"]:
                aggregated[name]["last_error_code"] = error_code
                aggregated[name]["last_failed_at"] = failed_at

    summary = [
        {
            "activity_name": name,
            "failure_count": data["failure_count"],
            "last_error_code": data["last_error_code"],
            "last_failed_at": data["last_failed_at"],
        }
        for name, data in sorted(aggregated.items(), key=lambda x: -x[1]["failure_count"])
    ]
    return {
        "pipeline_name": pipeline_name,
        "runs_checked": len(failed_runs),
        "failed_activity_summary": summary,
    }


def rerun_pipeline(
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    parameters: dict | None = None,
) -> dict:
    """
    Rerun a pipeline. This function itself has no authorization logic — gating happens one
    layer up, in RBACGateway.call()'s allowed/requires_consent check (rbac_permissions table,
    no role dimension). Called two ways today: through RBACGateway (gated) from the chat/
    investigator path, or directly from mcp_servers/adf/server.py's stdio tool dispatch
    (NOT gated — a real, tracked gap, see need_to_implement.txt).
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    run = client.pipelines.create_run(
        resource_group, factory_name, pipeline_name,
        parameters=parameters or {},
    )
    return {"new_run_id": run.run_id}


# --- Ported from claude-desktop/mcp_adf/tools/pipelines.py, read-only / action tools below
# (no checkpoint involvement, stay synchronous exactly like the source) ---

def list_pipelines(
    factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """List all pipeline names in the data factory."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    pipelines = client.pipelines.list_by_factory(resource_group, factory_name)
    return {"pipelines": [p.name for p in pipelines]}


def list_activity_runs(
    run_id: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    days: int = 30,
) -> dict:
    """
    Lists every activity in a specific pipeline run — name, type, status, timing, and
    activity_run_id for each. Deliberately lightweight: does NOT include input/output (can
    be large) — call get_activity_run_io with a specific activity_run_id from this list to
    see full input/output for just the activities that matter.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(timezone.utc)
    activity_filter = RunFilterParameters(
        last_updated_after=now - timedelta(days=days),
        last_updated_before=now,
    )
    activities = client.activity_runs.query_by_pipeline_run(resource_group, factory_name, run_id, activity_filter)
    return {
        "run_id": run_id,
        "activities": [
            {
                "activity_run_id": a.activity_run_id,
                "activity_name": a.activity_name,
                "activity_type": a.activity_type,
                "status": a.status,
                "start": str(a.activity_run_start),
                "start_ist": _to_ist(a.activity_run_start),
                "end": str(a.activity_run_end),
                "end_ist": _to_ist(a.activity_run_end),
                "duration_in_ms": a.duration_in_ms,
            }
            for a in activities.value
        ],
    }


def get_activity_run_io(
    activity_run_id: str, run_id: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    days: int = 30,
) -> dict:
    """
    Raw input/output payload for one specific activity run. ADF has no "get activity run by
    id" API — this queries every activity run for the given pipeline run_id within the last
    `days` days and picks out the one matching activity_run_id (both ids are already
    surfaced by get_activity_run_error's leaf/execution_path).
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(timezone.utc)
    activity_filter = RunFilterParameters(
        last_updated_after=now - timedelta(days=days),
        last_updated_before=now,
    )
    activities = client.activity_runs.query_by_pipeline_run(resource_group, factory_name, run_id, activity_filter)
    for a in activities.value:
        if a.activity_run_id == activity_run_id:
            return {
                "activity_run_id": activity_run_id,
                "activity_name": a.activity_name,
                "activity_type": a.activity_type,
                "status": a.status,
                "input": a.input,
                "output": a.output,
                "error": a.error,
            }
    return {"error": "activity_run_not_found", "activity_run_id": activity_run_id, "run_id": run_id}


def list_pipeline_runs(
    factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    hours: int = 24,
) -> dict:
    """Factory-wide run sweep — every pipeline's runs in the window, like ADF Studio's Monitor tab."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    now = datetime.now(timezone.utc)
    run_filter = RunFilterParameters(
        last_updated_after=now - timedelta(hours=hours),
        last_updated_before=now,
    )
    runs = client.pipeline_runs.query_by_factory(resource_group, factory_name, run_filter)
    return {
        "runs": [
            {
                "pipeline_name": r.pipeline_name,
                "run_id": r.run_id,
                "status": r.status,
                "start": str(r.run_start),
                "start_ist": _to_ist(r.run_start),
                "end": str(r.run_end),
                "end_ist": _to_ist(r.run_end),
                "triggered_by": {
                    "name": getattr(r.invoked_by, "name", None),
                    "type": getattr(r.invoked_by, "invoked_by_type", None),
                } if r.invoked_by else None,
            }
            for r in runs.value
        ]
    }


def get_pipeline_definition_raw(
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Full pipeline definition — every activity's typeProperties (dataset/linked-service
    references, queries, source/sink settings), policy (timeout/retry), and dependsOn. The
    editable structure to feed back into update_pipeline_definition once a fix is decided.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    pipeline = client.pipelines.get(resource_group, factory_name, pipeline_name)
    return _to_wire_dict(pipeline)


def cancel_pipeline_run(
    run_id: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
) -> dict:
    """
    Cancels a running (or hung) pipeline run. Needed before retrying a fix if a prior
    rerun_pipeline call is stuck rather than cleanly failed — otherwise runs pile up.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    client.pipeline_runs.cancel(resource_group, factory_name, run_id, is_recursive=True)
    return {"run_id": run_id, "cancelled": True, "reason": reason}


# --- Checkpoint-enabled tools below — async, since mcp_servers/adf/tools/_checkpoints.py's
# Postgres access is async-only (see that module's docstring). RBACGateway._dispatch()
# detects this via iscoroutinefunction and injects db/project as gateway-level context —
# never exposed in these functions' tool schemas, so an agent can't supply them directly.
# The actual Azure SDK calls inside are still synchronous, wrapped in run_in_executor. ---

async def create_pipeline(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    state_name: str | None = None,
) -> dict:
    """
    Creates a brand-new pipeline. Fails with an explicit error if a pipeline with this name
    already exists — use update_pipeline_definition to modify an existing pipeline instead.
    Records two checkpoints: "before-creation" (the resource didn't exist — rolling back
    here deletes it) and one for the just-created content, named `state_name` if given
    (default "created").

    `definition` accepts either the flat shape get_pipeline_definition_raw/
    update_pipeline_definition use, or the ARM/Studio-export shape ({"name": ...,
    "properties": {...}}) — if a "properties" key is present, its contents are used and the
    wrapper (including its own "name") is discarded. `pipeline_name` always determines the
    actual name created.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    try:
        await loop.run_in_executor(None, lambda: client.pipelines.get(resource_group, factory_name, pipeline_name))
        return {"error": "pipeline_already_exists", "pipeline_name": pipeline_name}
    except ResourceNotFoundError:
        pass

    await ck._push_snapshot(
        db, project, _KIND, pipeline_name,
        state_name="before-creation", action="create", reason=reason, change_summary="pipeline did not exist",
    )

    properties = definition.get("properties", definition)
    error = _reject_if_dropped_fields(properties, PipelineResource, "pipeline")
    if error:
        return error
    pipeline_resource = PipelineResource.deserialize(properties)
    error = _reject_if_miscased(pipeline_resource, "pipeline")
    if error:
        return error
    created = await loop.run_in_executor(
        None, lambda: client.pipelines.create_or_update(resource_group, factory_name, pipeline_name, pipeline_resource)
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, pipeline_name,
        state_name=state_name or "created", action="exists", reason=reason,
        change_summary="pipeline created", definition=_to_wire_dict(created),
    )
    await db.commit()
    return {
        "pipeline_name": pipeline_name,
        "created": True,
        "reason": reason,
        "saved_state_name": saved["state_name"],
        "etag": created.etag,
    }


async def update_pipeline_definition(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    definition: dict,
    reason: str,
    change_summary: str,
    state_name: str | None = None,
) -> dict:
    """
    Overwrites the pipeline's full definition (ADF has no "patch one activity" API —
    create_or_update replaces the whole activities array). If this pipeline has no history
    yet, its as-found content is captured as an "initial" checkpoint first. After applying
    the change, the NEW (resulting) content is pushed as a named checkpoint — so
    `state_name` (or its default) always names the state you're moving TO, and
    rollback_pipeline_definition(state_name=<that name>) restores exactly this resulting
    content, not what came before it.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    current = await loop.run_in_executor(None, lambda: client.pipelines.get(resource_group, factory_name, pipeline_name))
    await ck._ensure_baseline(db, project, _KIND, pipeline_name, _to_wire_dict(current), reason)

    error = _reject_if_dropped_fields(definition, PipelineResource, "pipeline")
    if error:
        return error
    pipeline_resource = PipelineResource.deserialize(definition)
    error = _reject_if_miscased(pipeline_resource, "pipeline")
    if error:
        return error
    updated = await loop.run_in_executor(
        None, lambda: client.pipelines.create_or_update(resource_group, factory_name, pipeline_name, pipeline_resource)
    )

    saved = await ck._push_snapshot(
        db, project, _KIND, pipeline_name,
        state_name=state_name, action="exists", reason=reason,
        change_summary=change_summary, definition=_to_wire_dict(updated),
    )
    await db.commit()
    return {
        "pipeline_name": pipeline_name,
        "updated": True,
        "reason": reason,
        "change_summary": change_summary,
        "saved_state_name": saved["state_name"],
        "etag": updated.etag,
    }


async def list_pipeline_snapshots(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """
    Lists every named checkpoint saved for this pipeline (state_name, timestamp, reason,
    change_summary), oldest first. Query this to see what's available before calling
    rollback_pipeline_definition with a specific state_name.
    """
    return {"pipeline_name": pipeline_name, "states": await ck.list_snapshots(db, project, _KIND, pipeline_name)}


async def rollback_pipeline_definition(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    state_name: str,
    confirm_delete: bool = False,
) -> dict:
    """
    Jumps directly to any named checkpoint from list_pipeline_snapshots, regardless of where
    it sits in history — nothing is ever deleted outright. For simple one-step undo/redo
    without needing a state_name, use back_pipeline_definition/forward_pipeline_definition
    instead.

    If the target checkpoint predates the pipeline's creation, applying it means DELETING
    the live pipeline — this returns {"requires_confirmation": true} instead of deleting;
    call again with confirm_delete=true only after the human has agreed to that.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._find_snapshot(db, project, _KIND, pipeline_name, state_name)
    if target is None:
        return {"error": "state_not_found", "pipeline_name": pipeline_name, "state_name": state_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.pipelines.delete(resource_group, factory_name, pipeline_name))

    async def apply_fn(target_definition: dict) -> None:
        error = _reject_if_dropped_fields(target_definition, PipelineResource, "pipeline")
        if error:
            raise ValueError(error)
        pipeline_resource = PipelineResource.deserialize(target_definition)
        error = _reject_if_miscased(pipeline_resource, "pipeline")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.pipelines.create_or_update(resource_group, factory_name, pipeline_name, pipeline_resource),
        )

    try:
        result = await ck._apply_rollback(db, project, _KIND, pipeline_name, target, reason, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if result.get("requires_confirmation"):
        result["pipeline_name"] = pipeline_name
        return result
    await db.commit()
    result["pipeline_name"] = pipeline_name
    return result


async def _pipeline_navigate(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str, direction: str, confirm_delete: bool,
) -> dict:
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    loop = asyncio.get_running_loop()

    target = await ck._step_snapshot(db, project, _KIND, pipeline_name, direction)
    if isinstance(target, dict):  # {"error": "no_history" | "no_earlier_state_available" | "no_later_state_available"}
        return {**target, "pipeline_name": pipeline_name}

    async def delete_fn() -> None:
        await loop.run_in_executor(None, lambda: client.pipelines.delete(resource_group, factory_name, pipeline_name))

    async def apply_fn(definition: dict) -> None:
        error = _reject_if_dropped_fields(definition, PipelineResource, "pipeline")
        if error:
            raise ValueError(error)
        pipeline_resource = PipelineResource.deserialize(definition)
        error = _reject_if_miscased(pipeline_resource, "pipeline")
        if error:
            raise ValueError(error)
        await loop.run_in_executor(
            None,
            lambda: client.pipelines.create_or_update(resource_group, factory_name, pipeline_name, pipeline_resource),
        )

    try:
        result = await ck._navigate(db, project, _KIND, pipeline_name, target, delete_fn, apply_fn, confirm_delete)
    except ValueError as exc:
        return exc.args[0]
    if not result.get("requires_confirmation"):
        await db.commit()
    result["pipeline_name"] = pipeline_name
    result["reason"] = reason
    return result


async def back_pipeline_definition(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """
    Moves one step back through this pipeline's history, like `git checkout HEAD~1` — the
    history log itself is untouched, only which checkpoint the live pipeline currently
    matches moves. Call forward_pipeline_definition to step forward again afterwards.

    If the step back lands on a point before this pipeline existed, applying it means
    DELETING the live pipeline — returns {"requires_confirmation": true} instead; call again
    with confirm_delete=true only after the human has agreed to that.
    """
    return await _pipeline_navigate(
        db, project, pipeline_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "back", confirm_delete,
    )


async def forward_pipeline_definition(
    db: AsyncSession, project: str,
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    reason: str,
    confirm_delete: bool = False,
) -> dict:
    """
    Moves one step forward through this pipeline's history — the mirror of
    back_pipeline_definition. Only meaningful after a previous back step; returns
    {"error": "no_later_state_available"} if the cursor is already at the newest checkpoint.
    """
    return await _pipeline_navigate(
        db, project, pipeline_name, factory_name, subscription_id, resource_group,
        tenant_id, client_id, client_secret, reason, "forward", confirm_delete,
    )
