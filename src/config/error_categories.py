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

