from datetime import datetime
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProjectMetadata(Base):
    __tablename__ = "project_metadata"

    # Stable internal identifier — normalised from event.project at intake
    project: Mapped[str] = mapped_column(String, primary_key=True)

    # Per-project Key Vault (Nexus's own tenant) holding this project's ADF client_secret.
    # Populated at onboarding, resolved by gateway/vault.py at credential-fetch time.
    key_vault_uri: Mapped[str | None] = mapped_column(String)

    # Non-secret ADF infra params — WatchTower no longer forwards these in the event payload
    # (it only sends `project` now), so they must be sourced from onboarding-time config here.
    adf_tenant_id: Mapped[str | None] = mapped_column(String)
    adf_client_id: Mapped[str | None] = mapped_column(String)
    adf_subscription_id: Mapped[str | None] = mapped_column(String)
    adf_resource_group: Mapped[str | None] = mapped_column(String)
    adf_factory_name: Mapped[str | None] = mapped_column(String)


class ProjectContact(Base):
    __tablename__ = "project_contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False, index=True)
    contact_type: Mapped[str] = mapped_column(String, nullable=False)  # primary_approver | escalation | on_call
    # The single person with chat access + notification target for this project
    # (Teams-specific fields and role-tiered approver fields dropped — chat access is
    # single-person-per-project with no role distinction, per the chat-product pivot).
    assigned_user_email: Mapped[str | None] = mapped_column(String)


class RBACPermission(Base):
    __tablename__ = "rbac_permissions"
    __table_args__ = (UniqueConstraint("role", "tool_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ProjectRCA(Base):
    __tablename__ = "project_rca"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False)
    error_signature: Mapped[str] = mapped_column(String, nullable=False)
    error_category: Mapped[str] = mapped_column(String, nullable=False)
    impact: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    fix_applied: Mapped[str | None] = mapped_column(Text)
    preventive_steps: Mapped[str | None] = mapped_column(Text)
    invocation_count: Mapped[int] = mapped_column(Integer, default=1)
    last_failure_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denial_history: Mapped[list] = mapped_column(JSON, default=list)
    last_rerun_attempt: Mapped[dict | None] = mapped_column(JSON)


class UserRole(Base):
    __tablename__ = "user_roles"

    # upn is the Teams user principal name (e.g. user@customer.com) — gateway looks up role by this key
    upn: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False)  # investigator | senior_eng | admin


class AuditLog(Base):
    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    pipeline_id: Mapped[str] = mapped_column(String, nullable=False)
    project: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)
    detail: Mapped[dict | None] = mapped_column(JSON)


class Investigation(Base):
    __tablename__ = "investigations"

    # Durable record of "a failure came in" — replaces LangGraph's implicit thread-state.
    # Intake writes this row immediately; run_diagnosis(investigation_id) is invoked later
    # (on a live "Diagnose" click, in the next milestone), not automatically at intake.
    investigation_id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String, nullable=False)
    run_status: Mapped[str] = mapped_column(String, nullable=False)
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    end_time: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    trigger_type: Mapped[str | None] = mapped_column(String)
    # pending_diagnosis | diagnosed | resolved
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_diagnosis")
    # Snapshot of run_diagnosis()'s final state fields (bucket, error_category, known_fix,
    # rca_id, investigation_summary, needs_approval, ...) so it doesn't need recomputing.
    diagnosis_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)