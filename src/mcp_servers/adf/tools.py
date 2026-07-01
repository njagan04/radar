from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from azure.mgmt.datafactory import DataFactoryManagementClient
from azure.mgmt.datafactory.models import RunFilterParameters, RunQueryFilter

from mcp_servers.adf.auth import get_credential


def _client(tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> DataFactoryManagementClient:
    return DataFactoryManagementClient(get_credential(tenant_id, client_id, client_secret), subscription_id)


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


def get_linked_service(
    service_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
) -> dict:
    """Evidence loop — connection details for a linked service."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    svc = client.linked_services.get(resource_group, factory_name, service_name)
    if svc is None:
        return {"name": service_name, "type": "unknown"}
    return {"name": svc.name, "type": svc.properties.type if svc.properties else "unknown"}


def rerun_pipeline(
    pipeline_name: str, factory_name: str,
    subscription_id: str, resource_group: str,
    tenant_id: str, client_id: str, client_secret: str,
    parameters: dict | None = None,
) -> dict:
    """RBAC-gated rerun. Only callable by senior_eng or admin role via RBAC gateway."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    run = client.pipelines.create_run(
        resource_group, factory_name, pipeline_name,
        parameters=parameters or {},
    )
    return {"new_run_id": run.run_id}


# Single source of truth for the ADF tool registry.
# Imported by both server.py (stdio MCP path) and rbac.py (in-process RBAC gateway path).
TOOL_REGISTRY: dict[str, Callable[..., dict]] = {
    "get_activity_run_error": get_activity_run_error,
    "get_pipeline_run_status": get_pipeline_run_status,
    "get_pipeline_run_history": get_pipeline_run_history,
    "get_activity_run_history": get_activity_run_history,
    "get_pipeline_definition": get_pipeline_definition,
    "get_linked_service": get_linked_service,
    "rerun_pipeline": rerun_pipeline,
}
