# Categories that cannot be resolved by an automated rerun — permanently human-only, by
# design (matches claude-desktop's R&D conclusion: no tool exists for these on purpose).
# Checked as a plain lookup against the investigator's own error_category output — not an
# LLM judgment call, since it doesn't need to be one.
HUMAN_ACTION_CATEGORIES: set[str] = {"credential_expired", "resource_unavailable", "platform_outage"}

ERROR_CATEGORIES: list[str] = [
    "oom",
    "timeout",
    "credential_expired",
    "permissions",           # service principal lacks RBAC rights (not expired, just missing)
    "network",
    "data_quality",
    "schema_drift",
    "resource_unavailable",
    "storage_access",
    "rate_limit",
    "config",
    "platform_outage",
    "business_logic",        # TO_IMPLEMENT: application-level failures (bad params, assertion errors)
    "cancelled",             # pipeline was cancelled (user/system/dependency)
    "unknown",
]

