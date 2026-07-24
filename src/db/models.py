from datetime import datetime
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProjectMetadata(Base):
    __tablename__ = "project_metadata"

    # Stable internal identifier — normalised from event.project at intake
    project: Mapped[str] = mapped_column(String, primary_key=True)

    # A project is always one platform (adf | synapse | databricks | fabric), even if it has
    # multiple factories/accounts on that platform. Populated at onboarding.
    platform: Mapped[str | None] = mapped_column(String)


class ProjectFactory(Base):
    """
    One row per platform instance (ADF factory / Databricks account / etc.) a project has.
    Current scope (2026-07-24): a project has exactly one instance, but the table stays
    general to support more later without another migration.

    Non-secret identifiers (tenant_id/client_id/subscription_id/resource_group/factory_name)
    are plain columns — they're identifiers, not credentials, and grant no access on their
    own. Only key_vault_uri points at a vault holding the one real secret: client-secret.
    """
    __tablename__ = "project_factories"
    __table_args__ = (UniqueConstraint("project", "factory_name", name="uq_project_factories_project_factory"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False, index=True)
    resource_group: Mapped[str] = mapped_column(String, nullable=False)
    factory_name: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String)
    client_id: Mapped[str | None] = mapped_column(String)
    subscription_id: Mapped[str | None] = mapped_column(String)
    key_vault_uri: Mapped[str | None] = mapped_column(String)


class ProjectContact(Base):
    __tablename__ = "project_contacts"
    __table_args__ = (UniqueConstraint("project", "contact_type", name="uq_project_contacts_project_type"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False, index=True)
    contact_type: Mapped[str] = mapped_column(String, nullable=False)  # primary_approver | escalation | on_call
    # Notification routing only — does NOT gate chat access (see UserProjectAccess).
    assigned_user_email: Mapped[str | None] = mapped_column(String)


class UserProjectAccess(Base):
    """
    Multi-user-per-project chat read access. Membership here grants read access to every
    chat thread under the project; write access is per-thread (ChatThread.claimed_by_user_email).
    Enforced via Postgres RLS (SET LOCAL app.current_user), not just app-level filtering.
    """
    __tablename__ = "user_project_access"

    user_email: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), primary_key=True)


class RBACPermission(Base):
    """
    Per-tool gate, no role dimension. `allowed` gates whether a tool can be called at all;
    `requires_consent` tiers whether the native chat UI shows an approve/deny dialog first.
    """
    __tablename__ = "rbac_permissions"

    tool_name: Mapped[str] = mapped_column(String, primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class ProjectRCA(Base):
    __tablename__ = "project_rca"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "project", "error_signature", name="uq_project_rca_pipeline_project_signature"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False)
    error_signature: Mapped[str] = mapped_column(String, nullable=False)
    # Cheap non-LLM lookup key, populated from error_detail.error_code. NOT part of the
    # uniqueness constraint — multiple rows may share an error_code with different,
    # more precise error_signatures.
    error_code: Mapped[str | None] = mapped_column(String, index=True)
    error_category: Mapped[str] = mapped_column(String, nullable=False)
    impact: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    fix_applied: Mapped[str | None] = mapped_column(Text)
    preventive_steps: Mapped[str | None] = mapped_column(Text)
    invocation_count: Mapped[int] = mapped_column(Integer, default=1)
    last_failure_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denial_history: Mapped[list] = mapped_column(JSON, default=list)
    last_rerun_attempt: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """
    Append-only, immutable — every row is self-contained (pipeline_id/project/platform stay
    denormalized on purpose, not joined from Investigation, so a row's meaning never changes
    even if the parent record's data changes later). No role column — actor is always the
    real assigned person's email once auth (item 17) lands; no more "system"/"investigator"
    placeholders.
    """
    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(
        String, ForeignKey("investigations.investigation_id"), nullable=False, index=True
    )
    pipeline_id: Mapped[str] = mapped_column(String, nullable=False)
    project: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str | None] = mapped_column(String)
    detail: Mapped[dict | None] = mapped_column(JSON)


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project", "factory_name"],
            ["project_factories.project", "project_factories.factory_name"],
            name="fk_investigations_project_factory",
        ),
    )

    # Durable record of "a failure came in", independent of whether anyone ever engages with
    # it. Intake writes this row immediately; run_diagnosis(investigation_id) is invoked later
    # (on a live "Diagnose" click), not automatically at intake.
    investigation_id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String, nullable=False)
    factory_name: Mapped[str | None] = mapped_column(String)
    run_status: Mapped[str] = mapped_column(String, nullable=False)
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    end_time: Mapped[str | None] = mapped_column(String)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    trigger_type: Mapped[str | None] = mapped_column(String)
    # pending_diagnosis | diagnosed | resolved
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_diagnosis")
    # Snapshot of run_diagnosis()'s final state fields (error_category, rca_id,
    # investigation_summary, needs_approval, ...) so it doesn't need recomputing.
    diagnosis_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatThread(Base):
    """
    NOT 1:1 with Investigation — a thread can be ad-hoc (project-scoped, no investigation)
    or failure-triggered (investigation_id set). claimed_by_user_email is set via a single
    atomic conditional UPDATE on first real write action (not on merely opening the thread),
    the standard optimistic-concurrency "claim" pattern. context_summary/
    summarized_through_message_id back LLM context-window management — LLM-input-only,
    never a substitute for the real chat_messages transcript.
    """
    __tablename__ = "chat_threads"

    thread_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False, index=True)
    investigation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("investigations.investigation_id"), unique=True
    )
    claimed_by_user_email: Mapped[str | None] = mapped_column(String, index=True)
    context_summary: Mapped[str | None] = mapped_column(Text)
    summarized_through_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chat_messages.id")
    )
    status: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(Base):
    """Append-only, full-fidelity, forever — the sole source of truth for both UI
    scrollback (cursor-paginated) and any context-summarization pass's input. Never rewritten."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chat_threads.thread_id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String, nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceSnapshotBlob(Base):
    """Content-addressed dedup store, git-blob-style."""
    __tablename__ = "resource_snapshot_blobs"

    hash: Mapped[str] = mapped_column(String, primary_key=True)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)


class ResourceSnapshot(Base):
    __tablename__ = "resource_snapshots"
    __table_args__ = (
        UniqueConstraint("project", "kind", "resource_name", "sequence", name="uq_resource_snapshots_identity"),
        CheckConstraint(
            "kind IN ('pipeline','dataset','linked_service','data_flow','trigger','global_parameter')",
            name="ck_resource_snapshots_kind",
        ),
        CheckConstraint("action IN ('exists','create')", name="ck_resource_snapshots_action"),
        CheckConstraint(
            "(action = 'create' AND blob_hash IS NULL) OR (action = 'exists' AND blob_hash IS NOT NULL)",
            name="ck_resource_snapshots_blob_hash_nullability",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String, ForeignKey("project_metadata.project"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    resource_name: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state_name: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    blob_hash: Mapped[str | None] = mapped_column(String, ForeignKey("resource_snapshot_blobs.hash"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceSnapshotCursor(Base):
    """
    Composite FK spans all four columns since `sequence` is only unique within one
    resource's own timeline. Implies insert order: a resource's first ResourceSnapshot
    row must exist before its cursor row can be created or moved to point at it.
    """
    __tablename__ = "resource_snapshot_cursor"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project", "kind", "resource_name", "current_sequence"],
            [
                "resource_snapshots.project",
                "resource_snapshots.kind",
                "resource_snapshots.resource_name",
                "resource_snapshots.sequence",
            ],
            name="fk_resource_snapshot_cursor_points_into_snapshots",
        ),
    )

    project: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    resource_name: Mapped[str] = mapped_column(String, primary_key=True)
    current_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
