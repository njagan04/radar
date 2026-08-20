"""
The two generic RCA tools every platform gets (check_known_fix, record_diagnosis_outcome) —
thin @function_tool wrappers around db/rca.py's plain query/write logic. Platform-agnostic:
ProjectRCA is keyed by (pipeline_id, project, error_signature), none of which are ADF-specific
concepts, so this has no dependency on mcp_servers/adf/.

Not routed through gateway.rbac.RBACGateway/call_tool:
  - These two tools are unconditionally available on every platform, including ones with zero
    RBACPermission rows at all (build_tools_for_platform adds them before any platform-specific
    dispatch even runs).
  - They never touch Azure/external infrastructure — both read and write are scoped to RADAR's
    own ProjectRCA bookkeeping table, so there is no mutating/destructive action here for a
    human approval flow to gate, unlike a pipeline rerun or a resource rollback.
  - _dispatch()'s generic (db, project, **arguments) calling convention has no way to carry the
    optional pipeline_id-defaults-to-state["pipeline_name"] resolution these two tools need,
    without colliding with an LLM-supplied `pipeline_id` argument of the same name.

Both calls are still fully audited (AuditLog rows below), under their own event types
(known_fix_lookup / diagnosis_outcome_recorded) rather than rbac_tool_call_allowed/denied.
"""

import json
from datetime import UTC, datetime

from agents import function_tool

from config.error_categories import ERROR_CATEGORIES
from db import rca as rca_db
from db.models import AuditLog
from llm.context import WorkflowContext
from llm.investigation_state import InvestigationState


def build_rca_tools(
    state: InvestigationState, ctx: WorkflowContext, user_id: str | None
) -> list:
    """check_known_fix/record_diagnosis_outcome — thin @function_tool wrappers around
    db/rca.py's plain query/write logic."""
    db_factory = ctx.db_factory
    _categories_line = ", ".join(ERROR_CATEGORIES)

    async def check_known_fix(
        pipeline_id: str | None = None, error_category: str | None = None
    ) -> str:
        """Check whether a known fix already exists — either in this exact pipeline's own RCA
        history, or (if error_category is given) on a different pipeline in this project that
        failed with the same error_category. This history is only as complete as prior calls
        to record_diagnosis_outcome — call that tool once you've diagnosed a failure so future
        lookups (here and in other chats) can find it.

        Args:
            pipeline_id: Which pipeline to check history for. In an ad-hoc conversation (this
                thread isn't tied to a specific failure), you MUST supply the real pipeline
                name the user is discussing — this thread has no structural pipeline context
                to fall back on, and omitting it here silently checks a meaningless placeholder
                instead of the pipeline actually being discussed. Leave unset only when this
                thread IS about a specific failure already (it defaults to that pipeline).
            error_category: Optional. If given, also searches other pipelines in this project
                that share this error_category, for cross-pipeline fix reuse. Use one of the
                canonical categories so matching actually works: {categories}.
        """
        resolved_pipeline_id = pipeline_id or state["pipeline_name"]
        async with db_factory() as db:
            same_pipeline_rows, cross_pipeline_row = await rca_db.find_known_fix(
                db,
                resolved_pipeline_id,
                state["project"],
                error_category,
            )

            payload = {
                "same_pipeline_history": [
                    {
                        "error_signature": r.error_signature,
                        "error_category": r.error_category,
                        "fix_applied": r.fix_applied,
                        "failure_count": r.failure_count,
                    }
                    for r in same_pipeline_rows
                ],
                "cross_pipeline_match": (
                    {
                        "source_pipeline_id": cross_pipeline_row.pipeline_id,
                        "error_category": cross_pipeline_row.error_category,
                        "fix_applied": cross_pipeline_row.fix_applied,
                    }
                    if cross_pipeline_row
                    else None
                ),
            }

            db.add(
                AuditLog(
                    investigation_id=state["investigation_id"],
                    thread_id=state["thread_id"],
                    pipeline_name=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                    timestamp=datetime.now(tz=UTC),
                    event_type="known_fix_lookup",
                    user_id=user_id,
                    # pipeline_name above stays the THREAD's own structural context (still
                    # "(ad-hoc)" for an ad-hoc thread, same as every other audit row) —
                    # resolved_pipeline_id records which pipeline was ACTUALLY looked up, which
                    # differs from it whenever the caller supplied an explicit pipeline_id.
                    detail={**payload, "resolved_pipeline_id": resolved_pipeline_id},
                )
            )
            await db.commit()

        return json.dumps(payload)

    check_known_fix.__doc__ = check_known_fix.__doc__.format(
        categories=_categories_line
    )
    check_known_fix = function_tool(check_known_fix)

    async def record_diagnosis_outcome(
        error_signature: str,
        error_category: str,
        pipeline_id: str | None = None,
        root_cause: str | None = None,
        fix_applied: str | None = None,
    ) -> str:
        """Record what you found once you've diagnosed this failure — this is what feeds
        check_known_fix (here and in future chats about this pipeline/project). Call this as
        soon as you have a real diagnosis, even if no fix was applied yet (e.g. root_cause
        alone is still useful). Safe to call more than once per thread as your understanding
        develops — it updates the same record rather than duplicating it.

        Args:
            error_signature: A short, stable identifier for this specific failure mode — used
                together with the pipeline and project to find/update the same record across
                calls and threads. If the failure has a concrete error_code (e.g. from
                error_detail.leaf.error_code or the failure context above), use that EXACT code
                as the signature — intake already logged a row under that same key the moment
                the failure arrived, and reusing it consolidates your diagnosis onto that row
                instead of fragmenting into a second one. Only fall back to a normalized form
                of the error message when no concrete code exists.
            error_category: The canonical category of this error — one of: {categories}. Using
                the canonical list (not a free-form variant) is what enables cross-pipeline fix
                reuse in check_known_fix.
            pipeline_id: Which pipeline this diagnosis is actually about. In an ad-hoc
                conversation (this thread isn't tied to a specific failure), you MUST supply
                the real pipeline name the user is discussing — this thread has no structural
                pipeline context to fall back on, and omitting it here files the record under
                a meaningless placeholder that no future lookup (here or check_known_fix, by
                either you or a future thread) will ever find under the real pipeline's name.
                Leave unset only when this thread IS about a specific failure already (it
                defaults to that pipeline).
            root_cause: What actually caused the failure, in your own words. Keep it to 1-2
                sentences — this gets resent as context in every future check_known_fix call
                for this pipeline, so verbose text compounds token cost over time.
            fix_applied: The fix that was applied or proposed, including any step that would
                prevent recurrence, if you have one. Same rule: 1-2 sentences, not a report.
        """
        resolved_pipeline_id = pipeline_id or state["pipeline_name"]
        async with db_factory() as db:
            row, created = await rca_db.record_diagnosis_outcome(
                db,
                pipeline_id=resolved_pipeline_id,
                project=state["project"],
                error_signature=error_signature,
                error_category=error_category,
                root_cause=root_cause,
                fix_applied=fix_applied,
            )

            db.add(
                AuditLog(
                    investigation_id=state["investigation_id"],
                    thread_id=state["thread_id"],
                    pipeline_name=state["pipeline_name"],
                    project=state["project"],
                    platform=state["platform"],
                    timestamp=datetime.now(tz=UTC),
                    event_type="diagnosis_outcome_recorded",
                    user_id=user_id,
                    detail={
                        "error_signature": error_signature,
                        "error_category": error_category,
                        "created": created,
                        "resolved_pipeline_id": resolved_pipeline_id,
                    },
                )
            )
            await db.commit()

        return json.dumps(
            {
                "recorded": True,
                "created": created,
                "pipeline_id": resolved_pipeline_id,
                "error_signature": error_signature,
            }
        )

    record_diagnosis_outcome.__doc__ = record_diagnosis_outcome.__doc__.format(
        categories=_categories_line
    )
    record_diagnosis_outcome = function_tool(record_diagnosis_outcome)

    return [check_known_fix, record_diagnosis_outcome]
