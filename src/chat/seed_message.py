"""
The seed message — a fixed, non-LLM template shown as the first chat_messages row in a
failure-triggered thread, giving a human useful context before they ask anything.
Deliberately not LLM-generated: fast, predictable, and doesn't cost a model call before the
human has even opened the chat.
"""

import re

from db.models import ProjectRCA

# ADF's own error message text embeds a descriptive code as "ErrorCode=<Name>" (e.g.
# "ErrorCode=MappingColumnNameNotFoundInSourceFile,...") — a more useful identifier than
# error_detail.error_code's raw numeric field (e.g. "2200"), which is a broad, generic
# activity-failure bucket reused across many unrelated failure types.
_ERROR_CODE_IN_MESSAGE = re.compile(r"ErrorCode=([A-Za-z0-9_]+)")


def extract_error_code(error_detail: dict | None) -> str | None:
    """Prefers the descriptive ErrorCode=<Name> embedded in the message text over the raw
    numeric error_code field — checked in both the nested "leaf" shape (nested ExecutePipeline
    chains put the actual failed activity's info there, not at the top level) and the flat
    top-level shape, before falling back to the numeric field for error shapes that don't have
    this message pattern at all."""
    if not error_detail:
        return None
    leaf = error_detail.get("leaf") or {}
    for message in (leaf.get("message"), error_detail.get("message")):
        if message:
            match = _ERROR_CODE_IN_MESSAGE.search(message)
            if match:
                return match.group(1)
    return leaf.get("error_code") or error_detail.get("error_code")


def build_seed_message(
    pipeline_name: str,
    project: str,
    run_status: str,
    last_error: str | None,
    matching_rca: ProjectRCA | None,
) -> str:
    """Takes plain fields (from InvestigationState, not a FailureEvent ORM row), matching what
    its caller has available at this point in the pipeline."""
    lines = [
        f"Pipeline `{pipeline_name}` ({project}) failed.",
        f"\nRun status: {run_status}",
    ]
    if last_error:
        lines.append(f"\nLast error: {last_error}")

    if matching_rca is not None:
        lines.append("")
        lines.append(
            f"\nThis exact error has been seen {matching_rca.failure_count} time(s) before."
        )
        if matching_rca.fix_applied:
            lines.append(f"\nPreviously applied fix: {matching_rca.fix_applied}")
    else:
        lines.append("")
        lines.append(
            "\nNo prior recorded fix for this exact error —run a full diagnosis."
        )

    return "\n".join(lines)
