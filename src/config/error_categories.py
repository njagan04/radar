# The canonical vocabulary for ProjectRCA.error_category (db/models.py) — passed to the chat
# agent via llm/tools.py's record_diagnosis_outcome/check_known_fix docstrings so the LLM
# stays consistent (e.g. always "timeout", never "Timeout"/"connection_timeout" as free-form
# variants), which is what makes check_known_fix's cross-pipeline error_category matching
# actually work. Not enforced as a hard schema constraint — just strongly guided.
ERROR_CATEGORIES: list[str] = [
    "oom",
    "timeout",
    "credential_expired",
    "permissions",  # service principal lacks RBAC rights (not expired, just missing)
    "network",
    "data_quality",
    "schema_drift",
    "resource_unavailable",
    "storage_access",
    "rate_limit",
    "config",
    "platform_outage",
    "business_logic",  # TO_IMPLEMENT: application-level failures (bad params, assertion errors)
    "cancelled",  # pipeline was cancelled (user/system/dependency)
    "unknown",
]

# Intake-time categorization — a small, deliberately conservative set of known ADF
# error-code substrings, used only to give a brand-new ProjectRCA row a starting category
# before any human has diagnosed it. record_diagnosis_outcome (llm/tools.py) unconditionally
# overwrites error_category on every real diagnosis, so a wrong/unknown guess here self-heals
# the first time someone actually chats about it — this never needs to be exhaustive.
_ERROR_CODE_CATEGORY_HINTS: dict[str, str] = {
    "mappingcolumnnamenotfound": "schema_drift",
    "schemamismatch": "schema_drift",
    "usererrorexceededmaxrecords": "data_quality",
    "invaliddatafound": "data_quality",
    "timeoutexpired": "timeout",
    "activitytimeoutexpired": "timeout",
    "executortimeout": "timeout",
    "sqlfailedtoconnect": "network",
    "connectiontimeout": "network",
    "hostunreachable": "network",
    "authenticationfailed": "credential_expired",
    "credentialexpired": "credential_expired",
    "tokenexpired": "credential_expired",
    "forbidden": "permissions",
    "accessdenied": "permissions",
    "unauthorized": "permissions",
    "throttl": "rate_limit",
    "toomanyrequests": "rate_limit",
    "outofmemory": "oom",
    "blobnotfound": "storage_access",
    "containernotfound": "storage_access",
    "notfound": "resource_unavailable",
    "cancelled": "cancelled",
}


def categorize_error_code(error_code: str | None) -> str:
    """Best-effort guess from the raw ADF error code alone (no LLM call) — 'unknown' is a
    correct, safe default, not a failure; see the hints dict's own docstring for why."""
    if not error_code:
        return "unknown"
    lowered = error_code.lower()
    for hint, category in _ERROR_CODE_CATEGORY_HINTS.items():
        if hint in lowered:
            return category
    return "unknown"
