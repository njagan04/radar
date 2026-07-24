import json
import logging
from datetime import datetime, timezone
from agents import (
    Agent,
    MaxTurnsExceeded,
    Runner,
    function_tool,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from config.error_categories import ERROR_CATEGORIES, HUMAN_ACTION_CATEGORIES
from config.settings import settings
from db.models import AuditLog, ProjectRCA
from gateway.rbac import RBACGateway, infra_params
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

logger = logging.getLogger(__name__)

# Azure AI Foundry's v1 endpoint — native Responses API, no forced Chat Completions mode needed.
_client = AsyncOpenAI(
    base_url=settings.azure_openai_v1_base_url,
    api_key=settings.azure_openai_api_key,
)

set_default_openai_client(_client, use_for_tracing=False)
set_tracing_disabled(True)  # no OpenAI platform account to receive Agents SDK traces

MAX_TURNS = 6


class InvestigationOutput(BaseModel):
    """Root cause analysis submitted once the investigator is confident in the findings."""

    error_signature: str = Field(
        description=(
            "Normalised error signature identifying this failure pattern. "
            "Format: <error_category>:<failed_activity>:<error_code>. "
            "Example: 'credential_expired:CopyData:UserErrorInvalidCredentials'"
        )
    )
    error_category: str = Field(
        description=f"Error category. Must be one of: {', '.join(ERROR_CATEGORIES)}."
    )
    root_cause: str = Field(description="Detailed explanation of the root cause based on gathered evidence.")
    impact: str = Field(description="Business impact of this failure (data freshness, downstream dependencies, SLA).")
    fix_applied: str = Field(description="Recommended remediation steps or the fix that was applied.")
    preventive_steps: str | None = Field(default=None, description="Steps to prevent recurrence.")

    @field_validator("error_category")
    @classmethod
    def _validate_error_category(cls, value: str) -> str:
        if value not in ERROR_CATEGORIES:
            raise ValueError(f"error_category must be one of {ERROR_CATEGORIES}, got {value!r}")
        return value


def _build_prior_history_section(state: InvestigationState) -> str:
    """
    Loop-prevention as passive context, not a hard pre-chat gate (§10 of
    implementation_plan.md) — surfaces the pipeline's most recent RCA (fetched cheaply by
    load_context.py, no LLM call) so the agent doesn't blindly recommend a fix that already
    failed, but the decision stays with the agent/human, not a Python branch.
    """
    prior = state.get("prior_rca_context")
    if not prior:
        return "\n## Prior History\nNo prior RCA on record for this pipeline.\n"

    lines = [
        "\n## Prior History (do not blindly repeat a fix that already failed)",
        f"Most recent error category: {prior.get('error_category')}",
        f"Most recent fix applied: {prior.get('fix_applied') or 'none recorded'}",
        f"Invocation count (times this exact error signature recurred): {prior.get('invocation_count')}",
        f"Denial count (times a human rejected the proposed fix): {prior.get('denial_count')}",
    ]
    last_rerun = prior.get("last_rerun_attempt")
    if last_rerun:
        lines.append(
            f"Last rerun attempt: outcome={last_rerun.get('outcome')}, "
            f"cleared_by_successful_run={last_rerun.get('cleared_by_successful_run')}"
        )
        if last_rerun.get("outcome") == "failed" and not last_rerun.get("cleared_by_successful_run"):
            lines.append(
                "WARNING: the last rerun for this pipeline FAILED and nothing has cleared it since. "
                "Use check_known_fix and check_upstream_dependencies before recommending another rerun."
            )
    return "\n".join(lines) + "\n"


def _build_system_prompt(state: InvestigationState) -> str:
    error_detail = state.get("error_detail") or {}

    # Build nested execution path section if available
    execution_path = error_detail.get("execution_path", [])
    if execution_path and len(execution_path) > 1:
        path_lines = " → ".join(
            f"{s['pipeline_name']}/{s['activity_name']} ({s['activity_type']})"
            for s in execution_path
        )
        nested_section = f"\n## Nested Execution Path\n{path_lines}\n"
        leaf = error_detail.get("leaf") or {}
        leaf_section = (
            f"Actual failed pipeline: {leaf.get('pipeline_name', 'unknown')}\n"
            f"Actual failed activity: {leaf.get('activity_name', 'unknown')} "
            f"(type: {leaf.get('activity_type', 'unknown')})\n"
            f"Error code: {leaf.get('error_code', 'unknown')}\n"
            f"Error message: {leaf.get('message', 'unknown')}\n"
        )
    else:
        nested_section = ""
        leaf_section = f"Error detail: {json.dumps(error_detail, indent=2) if error_detail else 'not provided'}\n"

    # Parallel failures: surface additional failed branches when multiple activities failed at once
    parallel_count = error_detail.get("parallel_failure_count", 1)
    extra_branches = error_detail.get("failed_branches", [])  # branches beyond the primary
    if parallel_count > 1 and extra_branches:
        branch_lines = "\n".join(
            f"  Branch {i + 2}: {b['leaf']['pipeline_name']}/{b['leaf']['activity_name']} "
            f"({b['leaf']['activity_type']}) — {b['leaf'].get('error_code', 'unknown')}"
            for i, b in enumerate(extra_branches)
            if b.get("leaf")
        )
        parallel_section = (
            f"\n## Parallel Failures ({parallel_count} branches failed simultaneously)\n"
            f"  Branch 1 (primary): see Root Failure Detail above\n"
            f"{branch_lines}\n"
            f"Investigate all branches — they may share a root cause or have independent causes.\n"
        )
    else:
        parallel_section = ""

    prior_history_section = _build_prior_history_section(state)

    return f"""You are an expert Azure Data Factory (ADF) pipeline failure investigator.

                Your job is to determine the root cause of a pipeline failure by gathering evidence using the available tools, then producing your final structured root cause analysis.

                ## Failure Context
                - Triggered pipeline: {state['pipeline_name']}
                - Project: {state['project']}
                - Platform: {state['platform']}
                - Failure time: {state['start_time']}
                - Run status: {state['run_status']}
                - Last error (brief): {state.get('last_error') or 'none'}
                {prior_history_section}{nested_section}{parallel_section}
                ## Root Failure Detail
                {leaf_section}
                ## Investigation approach
                1. Use check_known_fix early — a matching root cause may already be on record for this pipeline or another one in the project, making the rest of the evidence loop unnecessary.
                2. Use check_upstream_dependencies to rule out (or confirm) that this failure is caused by an upstream pipeline that also failed — don't duplicate an investigation that belongs to the real root cause.
                3. Use get_pipeline_run_history on the ACTUAL failed pipeline (not necessarily the triggered pipeline — see nested path above) to understand failure frequency.
                4. Use get_activity_run_history to identify which specific activities fail repeatedly and with what error codes.
                5. Use get_pipeline_definition on the actual failed pipeline to understand its structure.
                6. Use get_linked_service if the error points to a connection or credential issue.
                7. Once you have enough evidence to confidently state the root cause, produce your final answer.

                ## Rules
                - The error_signature must use the LEAF activity name and error code, not the Execute Pipeline activity.
                Format: <category>:<leaf_activity_name>:<error_code>
                - fix_applied should be actionable — what a data engineer would actually do to resolve this.
                - If check_upstream_dependencies confirms an upstream cascade, say so plainly in root_cause and attribute the fix to the upstream pipeline, not this one.
                - Only produce your final answer once you are confident. Keep gathering evidence with tools until then.
                """


async def investigator(state: InvestigationState, ctx: WorkflowContext) -> dict:
    db_factory = ctx.db_factory
    redis = ctx.redis

    async def _call_gateway(tool_name: str, arguments: dict) -> str:
        try:
            async with db_factory() as db:
                gateway = RBACGateway(
                    db=db,
                    redis=redis,
                    investigation_id=state["investigation_id"],
                    infra_params=infra_params(state),
                )
                result = await gateway.call(
                    tool_name=tool_name,
                    arguments=arguments,
                    actor="system",
                    pipeline_id=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                )
            return json.dumps(result)
        except PermissionError as exc:
            return str(exc)
        except Exception as exc:
            logger.exception("Tool call failed: tool=%s", tool_name)
            return f"Tool error: {exc}"

    @function_tool
    async def get_pipeline_run_history(pipeline_name: str, days: int = 7) -> str:
        """Get recent run history for a pipeline to identify failure patterns and frequency.

        Args:
            pipeline_name: Name of the ADF pipeline.
            days: Number of days of history to retrieve (1-30).
        """
        return await _call_gateway("get_pipeline_run_history", {"pipeline_name": pipeline_name, "days": days})

    @function_tool
    async def get_activity_run_history(pipeline_name: str, days: int = 7) -> str:
        """Aggregated summary of which activities have failed in recent runs -- failure count and
        last error code per activity. Use this to quickly identify if a specific activity is a
        chronic failure point.

        Args:
            pipeline_name: Name of the ADF pipeline.
            days: Number of days to look back (1-30).
        """
        return await _call_gateway("get_activity_run_history", {"pipeline_name": pipeline_name, "days": days})

    @function_tool
    async def get_pipeline_definition(pipeline_name: str) -> str:
        """Get the pipeline definition to understand its structure, activities, and dependencies.

        Args:
            pipeline_name: Name of the ADF pipeline.
        """
        return await _call_gateway("get_pipeline_definition", {"pipeline_name": pipeline_name})

    @function_tool
    async def get_linked_service(service_name: str) -> str:
        """Get connection details for a linked service referenced in the pipeline.

        Args:
            service_name: Name of the ADF linked service.
        """
        return await _call_gateway("get_linked_service", {"service_name": service_name})

    @function_tool
    async def check_known_fix(error_category: str | None = None) -> str:
        """Check whether a known fix already exists — either in this exact pipeline's own RCA
        history, or (if error_category is given) on a different pipeline in this project that
        failed with the same error_category. Ported from the retired classifier node: known-fix
        reuse is now the agent's own call to make, not a pre-chat routing decision. A denial_count
        above the configured threshold means humans have already rejected that fix repeatedly —
        treat it as a strong signal against reusing it again, not a hard block.

        Args:
            error_category: Optional. If given, also searches other pipelines in this project
                that share this error_category, for cross-pipeline fix reuse.
        """
        async with db_factory() as db:
            same_pipeline_result = await db.execute(
                select(ProjectRCA)
                .where(
                    ProjectRCA.pipeline_id == state["pipeline_name"],
                    ProjectRCA.project == state["project"],
                )
                .order_by(ProjectRCA.last_failure_timestamp.desc())
                .limit(5)
            )
            same_pipeline_rows: list[ProjectRCA] = list(same_pipeline_result.scalars().all())

            cross_pipeline_row: ProjectRCA | None = None
            if error_category:
                cross_result = await db.execute(
                    select(ProjectRCA)
                    .where(
                        ProjectRCA.project == state["project"],
                        ProjectRCA.pipeline_id != state["pipeline_name"],
                        ProjectRCA.error_category == error_category,
                        ProjectRCA.fix_applied.isnot(None),
                    )
                    .order_by(ProjectRCA.last_failure_timestamp.desc())
                    .limit(1)
                )
                cross_pipeline_row = cross_result.scalar_one_or_none()

        payload = {
            "same_pipeline_history": [
                {
                    "error_signature": r.error_signature,
                    "error_category": r.error_category,
                    "fix_applied": r.fix_applied,
                    "invocation_count": r.invocation_count,
                    "denial_count": len(r.denial_history or []),
                    "denial_threshold_reached": len(r.denial_history or []) >= settings.denial_threshold,
                }
                for r in same_pipeline_rows
            ],
            "cross_pipeline_match": (
                {
                    "source_pipeline_id": cross_pipeline_row.pipeline_id,
                    "error_category": cross_pipeline_row.error_category,
                    "fix_applied": cross_pipeline_row.fix_applied,
                }
                if cross_pipeline_row else None
            ),
        }

        async with db_factory() as db:
            db.add(AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(tz=timezone.utc),
                event_type="known_fix_lookup",
                actor="investigator",
                detail=payload,
            ))
            await db.commit()

        return json.dumps(payload)

    @function_tool
    async def check_upstream_dependencies(pipeline_name: str) -> str:
        """Discover ExecutePipeline references in a pipeline's definition (auto-discovered, not
        manually declared) and check whether any of those upstream pipelines also failed
        recently — evidence this failure is a cascade, not an independent root cause. Ported
        from the retired dependency_check node: cascade attribution is now the agent's own call,
        surfaced as evidence rather than a hard pre-chat routing decision.

        Args:
            pipeline_name: Name of the pipeline to check upstream dependencies for (usually the
                actual failed pipeline from the nested execution path, not necessarily the
                triggered one).
        """
        discovered_upstreams: list[str] = []
        try:
            definition_raw = await _call_gateway("get_pipeline_definition", {"pipeline_name": pipeline_name})
            definition = json.loads(definition_raw)
            discovered_upstreams = [
                a["references_pipeline"]
                for a in definition.get("activities", [])
                if a.get("type") == "ExecutePipeline" and a.get("references_pipeline")
            ]
        except Exception:
            logger.warning(
                "get_pipeline_definition failed in check_upstream_dependencies: investigation_id=%s",
                state["investigation_id"],
            )

        all_upstreams = list(dict.fromkeys(discovered_upstreams))  # preserves order, dedupes
        upstream_that_failed: str | None = None

        for upstream_pipeline in all_upstreams:
            try:
                history_raw = await _call_gateway(
                    "get_pipeline_run_history", {"pipeline_name": upstream_pipeline, "days": 1}
                )
                runs = json.loads(history_raw).get("runs", [])
                if any(
                    r.get("status") == "Failed" and r.get("start", "") <= state["start_time"]
                    for r in runs
                ):
                    upstream_that_failed = upstream_pipeline
                    break
            except Exception:
                logger.warning(
                    "Failed to check run history for upstream=%s investigation_id=%s",
                    upstream_pipeline, state["investigation_id"],
                )

        payload = {
            "discovered_upstreams": all_upstreams,
            "upstream_that_failed": upstream_that_failed,
            "is_cascade": upstream_that_failed is not None,
        }

        async with db_factory() as db:
            db.add(AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=pipeline_name,
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(tz=timezone.utc),
                event_type="dependency_check_lookup",
                actor="investigator",
                detail=payload,
            ))
            await db.commit()

        return json.dumps(payload)

    agent = Agent(
        name="adf_investigator",
        instructions=_build_system_prompt(state),
        tools=[
            check_known_fix,
            check_upstream_dependencies,
            get_pipeline_run_history,
            get_activity_run_history,
            get_pipeline_definition,
            get_linked_service,
        ],
        output_type=InvestigationOutput,
        model=settings.azure_openai_deployment,
    )

    rca_result: InvestigationOutput | None = None
    turns_used = MAX_TURNS
    try:
        run_result = await Runner.run(agent, "Begin investigation.", max_turns=MAX_TURNS)
        rca_result = run_result.final_output
        turns_used = len(run_result.raw_responses) or MAX_TURNS
    except MaxTurnsExceeded:
        rca_result = None

    if rca_result is None:
        # Exhausted turns without a final structured RCA — escalate
        async with db_factory() as db:
            db.add(AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(tz=timezone.utc),
                event_type="evidence_loop_max_iterations_reached",
                actor="investigator",
                detail={"turns_used": MAX_TURNS},
            ))
            await db.commit()
        return {
            "rca_id": None,
            "investigation_summary": (
                f"Investigation inconclusive after {MAX_TURNS} turns. Manual review required."
            ),
        }

    # Upsert into project_rca
    error_signature = rca_result.error_signature
    error_category = rca_result.error_category or state.get("error_category") or "unknown"
    now = datetime.now(tz=timezone.utc)

    async with db_factory() as db:
        result = await db.execute(
            select(ProjectRCA).where(
                ProjectRCA.pipeline_id == state["pipeline_name"],
                ProjectRCA.project == state["project"],
                ProjectRCA.error_signature == error_signature,
            )
        )
        existing: ProjectRCA | None = result.scalar_one_or_none()

        if existing:
            existing.root_cause = rca_result.root_cause
            existing.impact = rca_result.impact
            existing.fix_applied = rca_result.fix_applied
            existing.preventive_steps = rca_result.preventive_steps
            existing.error_category = error_category
            existing.last_failure_timestamp = now
            existing.invocation_count = (existing.invocation_count or 0) + 1
            rca_id = existing.id
        else:
            rca = ProjectRCA(
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                error_signature=error_signature,
                error_category=error_category,
                root_cause=rca_result.root_cause,
                impact=rca_result.impact,
                fix_applied=rca_result.fix_applied,
                preventive_steps=rca_result.preventive_steps,
                last_failure_timestamp=now,
                invocation_count=1,
                denial_history=[],
                last_rerun_attempt=None,
            )
            db.add(rca)
            await db.flush()
            rca_id = rca.id

        db.add(AuditLog(
            investigation_id=state["investigation_id"],
            pipeline_id=state["pipeline_name"],
            project=state["project"],
            platform=state["platform"],
            timestamp=now,
            event_type="investigation_completed",
            actor="investigator",
            detail={
                "rca_id": rca_id,
                "error_signature": error_signature,
                "error_category": error_category,
                "turns_used": turns_used,
            },
        ))
        await db.commit()

    summary = (
        f"**Root cause**: {rca_result.root_cause}\n\n"
        f"**Impact**: {rca_result.impact}\n\n"
        f"**Fix**: {rca_result.fix_applied}"
    )
    if rca_result.preventive_steps:
        summary += f"\n\n**Prevention**: {rca_result.preventive_steps}"

    # Plain lookup, not an LLM judgment call — some categories are permanently human-only by
    # design (no tool exists for these on purpose). Was the classifier's job pre-chat; now
    # computed straight from the investigator's own error_category output.
    requires_human_action = error_category in HUMAN_ACTION_CATEGORIES

    return {
        "rca_id": rca_id,
        "investigation_summary": summary,
        "error_category": error_category,
        "requires_human_action": requires_human_action,
    }
