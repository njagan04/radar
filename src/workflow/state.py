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
    # --- ADF infra params from event payload (non-secrets, safe in state/checkpoint) ---
    adf_tenant_id: str | None
    adf_client_id: str | None
    adf_subscription_id: str | None
    adf_resource_group: str | None
    adf_factory_name: str | None
    # --- set by pre_check ---
    has_prior_history: bool | None
    # --- set by classifier ---
    classification_bucket: int | None   # 0=loop_prevention 1=known_fix 2=cascade(dependency_check) 3=new/flapping_new
    classification_reasoning: str | None
    error_category: str | None
    known_fix: str | None
    requires_human_action: bool | None  # True when error_category demands manual intervention
    cross_pipeline_match: bool | None   # True when bucket=1 came from a different pipeline's RCA
    cross_pipeline_source: str | None   # pipeline_id that held the matching cross-pipeline RCA
    # --- set by dependency_check ---
    upstream_also_failed: bool | None
    # --- set by investigator ---
    rca_id: int | None
    investigation_summary: str | None
    # --- set by notifier ---
    notify_sent: bool | None
    needs_approval: bool | None
    # --- approval_action set by the future chat approve/deny endpoint; proposed_at set by notifier ---
    approval_action: str | None         # "approve" | "deny"
    proposed_at: str | None             # ISO-8601 timestamp when notifier proposed the fix
