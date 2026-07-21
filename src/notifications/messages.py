"""
Pure message-body builders — no delivery. Teams webhook/HMAC-approval-card delivery is
dropped entirely per the chat-product pivot (single-person-per-project, native in-chat
consent instead of an external approval channel). These functions keep the same per-bucket
message copy the old Teams cards used; actual delivery (the Outlook deep-link email, the
chat seed message) is built in the next milestone on top of these strings.
"""


def cancelled_body(pipeline_name: str, project: str) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) was cancelled and did not complete normally. "
        f"If this was unexpected, check the ADF run history for the cancellation source. "
        f"No automated rerun will be triggered for a cancelled run."
    )


def human_action_required_body(pipeline_name: str, project: str, error_category: str | None, last_error: str | None) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) requires manual intervention. "
        f"Error category: {error_category or 'unknown'}. "
        f"This error type cannot be resolved through automated rerun. "
        f"A data engineer must action this directly. "
        f"Details: {last_error or 'See ADF for full error.'}"
    )


def loop_prevention_body(pipeline_name: str, project: str, error_category: str | None) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) has entered a rerun loop. "
        f"A prior rerun failed with the same error category (`{error_category or 'unknown'}`) "
        f"and no successful run has cleared it. Manual investigation required — do not trigger "
        f"another rerun until the root cause is resolved."
    )


def cascade_confirmed_body(pipeline_name: str, project: str) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) failed due to an upstream cascade. "
        f"An upstream pipeline also failed before this run started. Fix the upstream pipeline "
        f"first — no rerun of this pipeline is warranted until the upstream is healthy."
    )


def cross_pipeline_known_fix_body(
    pipeline_name: str, project: str, error_category: str | None, source_pipeline: str | None, known_fix: str | None
) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) failed with a known error that affected another "
        f"pipeline in this project. Error category: {error_category or 'unknown'}. "
        f"Source pipeline: `{source_pipeline or 'unknown'}`. Known fix: {known_fix or 'N/A'}. "
        f"Approve to trigger an automatic rerun using the same fix."
    )


def known_fix_body(pipeline_name: str, project: str, error_category: str | None, known_fix: str | None) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) failed with a previously seen error. "
        f"Error category: {error_category or 'unknown'}. Known fix: {known_fix or 'N/A'}. "
        f"Approve to trigger an automatic rerun."
    )


def rca_complete_body(pipeline_name: str, project: str, summary: str | None) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) failed. An automated investigation has completed.\n\n"
        f"{summary or 'Investigation complete — see audit log for details.'}\n\n"
        f"Approve to trigger an automatic rerun."
    )


def batch_alert_body(platform: str, failure_count: int, window_minutes: int, recent_projects: list[str]) -> str:
    projects_str = ", ".join(recent_projects[:5])
    if len(recent_projects) > 5:
        projects_str += f" (+{len(recent_projects) - 5} more)"
    return (
        f"{failure_count} pipeline failures detected on {platform.upper()} within the last "
        f"{window_minutes} minutes. Affected projects: {projects_str}. This may indicate a "
        f"platform-wide issue — individual investigations have been suppressed."
    )


def rerun_outcome_body(pipeline_name: str, outcome: str, new_run_id: str | None) -> str:
    if outcome == "succeeded":
        body = "The pipeline rerun completed successfully."
        if new_run_id:
            body += f" Run ID: {new_run_id}"
        return body
    if outcome == "failed":
        body = "The pipeline rerun failed again. Manual investigation is required."
        if new_run_id:
            body += f" Run ID: {new_run_id}"
        return body
    return (
        "The pipeline rerun was triggered but its outcome could not be determined within "
        "the monitoring window. Check ADF directly."
    )
