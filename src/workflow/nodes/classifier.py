from datetime import datetime, timezone

import json

from openai import AsyncOpenAI
from sqlalchemy import select

from config.error_categories import ERROR_CATEGORIES
from config.settings import settings
from db.models import AuditLog, ProjectRCA
from workflow.context import WorkflowContext
from workflow.state import InvestigationState

_client = AsyncOpenAI(
    base_url=settings.azure_openai_v1_base_url,
    api_key=settings.azure_openai_api_key,
)

# Error categories that cannot be resolved by automated rerun — require manual intervention.
_HUMAN_ACTION_CATEGORIES = {"credential_expired", "resource_unavailable", "platform_outage"}

_CLASSIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "classify_failure",
        "description": "Classify the pipeline failure into a bucket and assign an error category.",
        "parameters": {
            "type": "object",
            "properties": {
                "bucket": {
                    "type": "integer",
                    "enum": [1, 3],
                    "description": (
                        "1 = flapping pipeline, same error signature as RCA history — known fix exists, no reinvestigation needed. "
                        "3 = new/ambiguous — either this pipeline hasn't failed this way before, or it's a known flapper "
                        "but the error signature is different. Full investigation including SOP retrieval required."
                    ),
                },
                "error_category": {
                    "type": "string",
                    "enum": ERROR_CATEGORIES,
                },
                "reasoning": {"type": "string"},
                "known_fix": {
                    "type": "string",
                    "description": "Only populate when bucket=1. The recommended fix from RCA history.",
                },
            },
            "required": ["bucket", "error_category", "reasoning"],
        },
    },
}


def _build_prompt(state: InvestigationState, rca_rows: list[ProjectRCA]) -> str:
    lines = [
        f"Pipeline: {state['pipeline_name']}",
        f"Project: {state['project']}",
        f"Platform: {state['platform']}",
        f"Last error: {state.get('last_error') or 'none'}",
        f"Failure count: {state['failure_count']}",
        f"Error detail: {json.dumps(state['error_detail']) if state.get('error_detail') else 'none'}",
        "",
    ]

    if rca_rows:
        lines.append("RCA history (most recent first):")
        for r in rca_rows:
            lines.append(
                f"  - error_signature={r.error_signature!r}"
                f"  category={r.error_category}"
                f"  root_cause={r.root_cause!r}"
                f"  fix_applied={r.fix_applied!r}"
                f"  invocations={r.invocation_count}"
                f"  denials={len(r.denial_history or [])}"
            )
    else:
        lines.append("RCA history: none")

    lines += [
        "",
        "Classify this failure using the classify_failure tool.",
        "Bucket 1: a known fix exists in RCA history and is directly applicable to this failure.",
        "Bucket 3: this is new, ambiguous, or a known flapper with a different error — needs full investigation.",
        "Only include known_fix when bucket=1.",
        "Note: dependency cascade detection is handled separately — do not classify as cascade here.",
    ]
    return "\n".join(lines)


def _loop_prevention_triggered(state: InvestigationState, top: ProjectRCA) -> bool:
    rerun = top.last_rerun_attempt
    if not rerun or rerun.get("outcome") != "failed":
        return False
    if rerun.get("cleared_by_successful_run", False):
        return False
    if rerun.get("error_category_before_rerun") != top.error_category:
        return False
    rerun_ts = rerun.get("timestamp", "")
    return bool(rerun_ts) and state["start_time"] > rerun_ts


async def classifier(state: InvestigationState, ctx: WorkflowContext) -> dict:
    db_factory = ctx.db_factory

    async with db_factory() as db:
        result = await db.execute(
            select(ProjectRCA)
            .where(
                ProjectRCA.pipeline_id == state["pipeline_name"],
                ProjectRCA.project == state["project"],
            )
            .order_by(ProjectRCA.last_failure_timestamp.desc())
            .limit(5)
        )
        rca_rows: list[ProjectRCA] = list(result.scalars().all())

    if rca_rows and _loop_prevention_triggered(state, rca_rows[0]):
        top = rca_rows[0]
        async with db_factory() as db:
            db.add(
                AuditLog(
                    investigation_id=state["investigation_id"],
                    pipeline_id=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                    timestamp=datetime.now(tz=timezone.utc),
                    event_type="loop_prevention_triggered",
                    actor="classifier",
                    detail={
                        "error_category": top.error_category,
                        "last_rerun_attempt": top.last_rerun_attempt,
                    },
                )
            )
            await db.commit()
        return {
            "classification_bucket": 0,
            "classification_reasoning": (
                "Loop prevention: prior rerun for this pipeline failed with the same "
                "error category and no successful run has cleared it."
            ),
            "error_category": top.error_category,
            "known_fix": None,
            "requires_human_action": None,
        }

    prompt = _build_prompt(state, rca_rows)
    response = await _client.chat.completions.create(
        model=settings.azure_openai_deployment,
        messages=[{"role": "user", "content": prompt}],
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "function", "function": {"name": "classify_failure"}},
    )

    tool_input: dict = json.loads(response.choices[0].message.tool_calls[0].function.arguments)

    bucket: int = tool_input["bucket"]
    error_category: str = tool_input["error_category"]
    reasoning: str = tool_input["reasoning"]
    known_fix: str | None = tool_input.get("known_fix") if bucket == 1 else None
    requires_human_action: bool = error_category in _HUMAN_ACTION_CATEGORIES

    # Denial history pre-check for bucket=1:
    # If this (pipeline, fix) has been denied >= denial_threshold times, override to bucket=3
    # so the full investigation path runs instead of resending the same known-fix card.
    if bucket == 1 and not requires_human_action:
        matched_rca = next(
            (r for r in rca_rows if r.fix_applied and r.fix_applied == known_fix),
            None,
        )
        if matched_rca:
            denial_count = len(matched_rca.denial_history or [])
            if denial_count >= settings.denial_threshold:
                bucket = 3
                known_fix = None
                reasoning = (
                    f"Denial threshold reached ({denial_count} denials for this fix). "
                    f"Overriding to full investigation. Original reasoning: {reasoning}"
                )

    # Cross-pipeline same-error check:
    # If bucket is still 3 (no same-pipeline match), look for any other pipeline in this
    # project that already has an RCA with the same error_category. If found, use that fix —
    # same root cause, no need to re-investigate. Example: Key Vault outage causes pipeline A
    # and pipeline B to fail with the same credential_expired category; B can reuse A's RCA.
    cross_pipeline_match = False
    cross_pipeline_source: str | None = None
    if bucket == 3 and not requires_human_action:
        async with db_factory() as db:
            xp_result = await db.execute(
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
            xp_rca: ProjectRCA | None = xp_result.scalar_one_or_none()

        if xp_rca:
            bucket = 1
            known_fix = xp_rca.fix_applied
            cross_pipeline_match = True
            cross_pipeline_source = xp_rca.pipeline_id
            reasoning = (
                f"Cross-pipeline match: same error_category '{error_category}' found in "
                f"pipeline '{xp_rca.pipeline_id}' (project={state['project']}). "
                f"Reusing known fix without re-investigation. "
                f"Original reasoning: {reasoning}"
            )

    async with db_factory() as db:
        db.add(
            AuditLog(
                investigation_id=state["investigation_id"],
                pipeline_id=state["pipeline_name"],
                project=state["project"],
                platform=state["platform"],
                timestamp=datetime.now(tz=timezone.utc),
                event_type="classification_decision",
                actor="classifier",
                detail={
                    "bucket": bucket,
                    "error_category": error_category,
                    "reasoning": reasoning,
                    "known_fix": known_fix,
                    "requires_human_action": requires_human_action,
                    "cross_pipeline_match": cross_pipeline_match,
                    "cross_pipeline_source": cross_pipeline_source,
                },
            )
        )
        await db.commit()

    return {
        "classification_bucket": bucket,
        "classification_reasoning": reasoning,
        "error_category": error_category,
        "known_fix": known_fix,
        "requires_human_action": requires_human_action,
        "cross_pipeline_match": cross_pipeline_match,
        "cross_pipeline_source": cross_pipeline_source,
    }
