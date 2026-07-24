from typing import TypedDict


class InvestigationState(TypedDict):
    investigation_id: str
    project: str
    platform: str
    pipeline_name: str
    run_status: str
    start_time: str
    end_time: str | None
    last_error: str | None
    error_detail: dict | None           # enriched by initial_evidence_fetch with full nested execution path + leaf failure
    failure_count: int
    trigger_type: str | None
    thread_status: str                  # "running" | "paused" | "failed" | "completed"
    # --- non-secret factory identifiers, sourced from project_factories (not a secret,
    #     no Key Vault fetch needed — plain columns, safe in state) ---
    tenant_id: str | None
    client_id: str | None
    subscription_id: str | None
    resource_group: str | None
    factory_name: str | None
    # --- set by pre_check (the one remaining hard pre-chat gate) ---
    # error_category is set to "cancelled" here when triggered; otherwise left None until
    # investigator sets it. classifier.py/dependency_check.py retired 2026-07-24 — known-fix
    # reuse and cascade detection are now tools the live investigator agent calls itself
    # (check_known_fix/check_upstream_dependencies), not pre-chat routing decisions.
    error_category: str | None
    # --- set by load_context — passive loop-prevention context, not a routing decision ---
    prior_rca_context: dict | None
    # --- set by investigator ---
    rca_id: int | None
    investigation_summary: str | None
    requires_human_action: bool | None  # True when error_category demands manual intervention
    # --- set by notifier ---
    notify_sent: bool | None
    needs_approval: bool | None
    # --- approval_action set by the future chat approve/deny endpoint; proposed_at set by notifier ---
    approval_action: str | None         # "approve" | "deny"
    proposed_at: str | None             # ISO-8601 timestamp when notifier proposed the fix
