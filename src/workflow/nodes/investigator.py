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

from config.error_categories import ERROR_CATEGORIES
from config.settings import settings
from db.models import AuditLog, ProjectRCA
from gateway.rbac import RBACGateway, adf_infra_params
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
        description=(
            "Error category — must match the classifier's categorisation if one exists. "
            f"Must be one of: {', '.join(ERROR_CATEGORIES)}."
        )
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

    return f"""You are an expert Azure Data Factory (ADF) pipeline failure investigator.

                Your job is to determine the root cause of a pipeline failure by gathering evidence using the available tools, then producing your final structured root cause analysis.

                ## Failure Context
                - Triggered pipeline: {state['pipeline_name']}
                - Project: {state['project']}
                - Platform: {state['platform']}
                - Failure time: {state['start_time']}
                - Run status: {state['run_status']}
                - Last error (brief): {state.get('last_error') or 'none'}
                - Prior classification: bucket={state.get('classification_bucket')}, category={state.get('error_category') or 'unknown'}
                {nested_section}{parallel_section}
                ## Root Failure Detail
                {leaf_section}
                ## Investigation approach
                1. Use get_pipeline_run_history on the ACTUAL failed pipeline (not necessarily the triggered pipeline — see nested path above) to understand failure frequency.
                2. Use get_activity_run_history to identify which specific activities fail repeatedly and with what error codes.
                3. Use get_pipeline_definition on the actual failed pipeline to understand its structure.
                4. Use get_linked_service if the error points to a connection or credential issue.
                5. Once you have enough evidence to confidently state the root cause, produce your final answer.

                ## Rules
                - The error_signature must use the LEAF activity name and error code, not the Execute Pipeline activity.
                Format: <category>:<leaf_activity_name>:<error_code>
                - error_category must match the classifier's category if one was assigned ({state.get('error_category') or 'unknown'}).
                - fix_applied should be actionable — what a data engineer would actually do to resolve this.
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
                    infra_params=adf_infra_params(state),
                )
                result = await gateway.call(
                    tool_name=tool_name,
                    arguments=arguments,
                    actor="system",
                    role="investigator",
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

    agent = Agent(
        name="adf_investigator",
        instructions=_build_system_prompt(state),
        tools=[get_pipeline_run_history, get_activity_run_history, get_pipeline_definition, get_linked_service],
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


    return {
        "rca_id": rca_id,
        "investigation_summary": summary,
        "error_category": error_category,
    }
