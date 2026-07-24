"""
Pure message-body builders for human-facing notifications — no delivery (see
notifications/email.py for that). One body regardless of investigation outcome: the email's
only job is getting the human into the chat, since the specifics (error category, root cause,
whether a rerun will even be offered) render once they open it, via the seed message.
"""


def investigation_notification_body(pipeline_name: str, project: str, chat_url: str) -> str:
    return (
        f"Pipeline `{pipeline_name}` ({project}) needs review.\n\n"
        f"Open the investigation: {chat_url}"
    )


def batch_alert_body(platform: str, failure_count: int, window_minutes: int, recent_projects: list[str]) -> str:
    """Unused while batch detection is switched off — kept for its possible future re-enablement."""
    projects_str = ", ".join(recent_projects[:5])
    if len(recent_projects) > 5:
        projects_str += f" (+{len(recent_projects) - 5} more)"
    return (
        f"{failure_count} pipeline failures detected on {platform.upper()} within the last "
        f"{window_minutes} minutes. Affected projects: {projects_str}. This may indicate a "
        f"platform-wide issue — individual investigations have been suppressed."
    )
