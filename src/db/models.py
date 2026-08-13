import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
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


class Credential(Base):
    """
    One row per platform instance (ADF factory / Databricks account / etc.) a project has. A
    project currently has exactly one instance, but the table stays general to support more
    without another migration.

    Non-secret identifiers (tenant_id/client_id/subscription_id/resource_group/factory_name)
    are plain columns — they're identifiers, not credentials, and grant no access on their
    own. The one real secret, client_secret, is not stored here at all — it's resolved per
    call straight from WatchTower's own public."Credential".clientSecret (see
    gateway/credential_resolution.py). tenant_id/client_id/subscription_id are NOT NULL: every
    code path that reads a Credential row (gateway/credential_resolution.py,
    chat/state_builder.py) treats them as required to build a working ADF client — a row
    missing them would fail confusingly at call time instead of being rejected at write time.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        UniqueConstraint(
            "project", "factory_name", name="uq_credentials_project_factory"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(
        String, ForeignKey("project_metadata.project"), nullable=False, index=True
    )
    resource_group: Mapped[str] = mapped_column(String, nullable=False)
    factory_name: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    subscription_id: Mapped[str] = mapped_column(String, nullable=False)


class RBACPermission(Base):
    """
    Per-tool gate, no role dimension. `allowed` gates whether a tool can be called at all;
    `requires_consent` tiers whether the native chat UI shows an approve/deny dialog first.
    """

    __tablename__ = "rbac_permissions"

    tool_name: Mapped[str] = mapped_column(String, primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Defense-in-depth for tool_search_tool.py's build_chat_tools: every real row is "adf"
    # today; filtering by this column means a future non-ADF caller gets zero rows (safe
    # no-op) instead of leaking these tools into an unrelated platform.
    platform: Mapped[str] = mapped_column(
        String, nullable=False, default="adf", server_default="adf"
    )


class ProjectRCA(Base):
    __tablename__ = "project_rca"
    __table_args__ = (
        # Also serves plain pipeline_id/project-only queries via the leftmost-prefix rule —
        # no standalone index needed for those.
        UniqueConstraint(
            "pipeline_id",
            "project",
            "error_signature",
            name="uq_project_rca_pipeline_project_signature",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String, nullable=False)
    project: Mapped[str] = mapped_column(
        String, ForeignKey("project_metadata.project"), nullable=False
    )
    error_signature: Mapped[str] = mapped_column(String, nullable=False)
    error_category: Mapped[str] = mapped_column(String, nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text)
    # Covers both what was done AND how to prevent recurrence. Content here gets resent as
    # context in every future check_known_fix call, so keep it short.
    fix_applied: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=1)
    last_failure_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """
    Append-only, immutable — every row is self-contained (pipeline_name/project/platform stay
    denormalized on purpose, not joined from FailureEvent, so a row's meaning never changes
    even if the parent record's data changes later).

    investigation_id is nullable — an ad-hoc chat thread has no FailureEvent to point at, but
    its tool calls still need auditing. Postgres skips FK enforcement on NULL. thread_id is
    the only column that ties a row to the specific chat the tool call happened in —
    investigation_id alone doesn't cover it, since it's NULL for every ad-hoc thread, which is
    most chat usage. Also nullable — some events (thread_setup's "notification_ready") aren't
    chat-turn-scoped at all.

    user_id is WatchTower's public."User".id, the same real identity
    ChatThread.claimed_by_user_id uses — never an email, and never used for authorization here
    either, purely for "who did this" when reading the log. Nullable: system-originated events
    (intake, the diagnosis pipeline) have no real user to attribute to.
    """

    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    investigation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("failure_events.investigation_id"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("chat_threads.thread_id"),
        nullable=True,
        index=True,
    )
    pipeline_name: Mapped[str] = mapped_column(String, nullable=False)
    project: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    detail: Mapped[dict | None] = mapped_column(JSON)


class FailureEvent(Base):
    """
    A row here is created the moment WatchTower posts a pipeline-failure event, whether or
    not a human ever opens it. investigation_id is the join key threaded through
    chat_threads/audit_log/notifications/workflow state everywhere else, and names that
    investigation's identity once a chat does start.
    """

    __tablename__ = "failure_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project", "factory_name"],
            ["credentials.project", "credentials.factory_name"],
            name="fk_failure_events_project_factory",
        ),
        Index(
            "ix_failure_events_project_pipeline_created",
            "project",
            "pipeline_name",
            "created_at",
        ),
    )

    # Durable record of "a failure came in", independent of whether anyone ever engages with
    # it. Intake writes this row immediately; run_diagnosis(investigation_id) is invoked later
    # (on a live "Diagnose" click), not automatically at intake.
    investigation_id: Mapped[str] = mapped_column(String, primary_key=True)
    # No standalone index — ix_failure_events_project_pipeline_created above already covers
    # plain project-only queries via the leftmost-prefix rule.
    project: Mapped[str] = mapped_column(
        String, ForeignKey("project_metadata.project"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String, nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String, nullable=False)
    factory_name: Mapped[str | None] = mapped_column(String)
    run_status: Mapped[str] = mapped_column(String, nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    trigger_type: Mapped[str | None] = mapped_column(String)
    # Written once at intake (chat/thread_setup.py), read by the notification bell and the
    # draft ("new") chat page. NULL once a real ChatThread has been created from it
    # (resolved_thread_id set) — nothing left pending at that point.
    seed_message: Mapped[str | None] = mapped_column(Text)
    resolved_thread_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_threads.thread_id")
    )
    # Distinct from resolved_thread_id: sending a message resolves a notification, but a
    # human can open/read one from the notification list without ever sending anything. Set
    # the moment that specific notification's row is clicked in the list (not just when the
    # list itself is opened) — drives the "new" vs "already looked at this" opacity
    # distinction. NULL means nobody's opened it yet.
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatThread(Base):
    """
    Not 1:1 with FailureEvent — a thread can be ad-hoc (project-scoped, no failure event) or
    failure-triggered (investigation_id set). A new ad-hoc thread ties to a project by the
    caller supplying `project` explicitly at creation (e.g. picked from the sidebar of
    projects the user has access to per WatchTower's UserProjectAssignment) — there's no
    other signal to infer it from. A failure-triggered thread instead inherits `project` from
    its FailureEvent row, so that association is automatic. claimed_by_user_id is set via a
    single atomic conditional UPDATE on first real write action (not on merely opening the
    thread), the standard optimistic-concurrency "claim" pattern.

    context_summary/summarized_through_timestamp back LLM context-window management — LLM-
    input-only, never a substitute for the real chat_messages transcript. summarized_through_
    timestamp is deliberately a plain timestamp, not a FK to a specific chat_messages.id:
    summarization only ever needs "give me messages after X," not a specific row's identity.

    pending_tool_approval is distinct from `status` — `status` stays scoped to the separate
    rerun-consent lifecycle (open -> awaiting_approval -> resolved); this column instead
    pauses one in-flight chat turn whose agent run hit a tool needing human approval (OpenAI
    Agents SDK's needs_approval mechanism). Shape: {"run_state": <RunState.to_json() output>,
    "pending_tools": [{"tool_call_id", "tool_name", "tool_arguments"}, ...],
    "triggering_message": <the user message this paused turn was built against - the resume
    path must rebuild the identical Agent/tool subset>, "created_at": <iso timestamp, for the
    90-minute TTL check>, "sdk_version": <installed openai-agents version, so a stale approval
    + upgraded SDK fails loud instead of deserializing into garbage>}. Nullable; null means no
    turn is currently paused.
    """

    __tablename__ = "chat_threads"

    # Python-side default (not server_default) so it works identically on both the real Postgres
    # column (which also has its own DEFAULT gen_random_uuid() from the migration — never
    # triggered since SQLAlchemy always supplies an explicit value) and the sqlite test fixture,
    # which has no gen_random_uuid() function at all.
    thread_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project: Mapped[str] = mapped_column(
        String, ForeignKey("project_metadata.project"), nullable=False, index=True
    )
    investigation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("failure_events.investigation_id"), unique=True
    )
    # Auto-set from the first user message (plain truncation, see chat/service.py's
    # post_message), renamable via PATCH /chat/threads/{id}.
    title: Mapped[str | None] = mapped_column(String)
    # Real identity, WatchTower's public.User.id (a true cross-schema FK, see the migration) —
    # carried directly in the verified X-Radar-Assertion JWT's `id` claim (chat/deps.py's
    # get_current_user_id).
    claimed_by_user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), index=True
    )
    # Soft delete: DELETE /chat/threads/{id} sets this instead of removing the row, so
    # nothing is ever actually lost. Every read path (_get_thread_or_404) excludes
    # is_deleted=true threads.
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    context_summary: Mapped[str | None] = mapped_column(Text)
    summarized_through_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    pending_tool_approval: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(Base):
    """
    Append-only, full-fidelity, forever — the sole source of truth for both UI scrollback
    (cursor-paginated) and any context-summarization pass's input. Never rewritten.

    role is CHECK-constrained to user/assistant only — tool calls made while producing an
    assistant reply are summarized into that same row's tool_calls JSON column instead of
    becoming their own rows.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        # Serves cursor-paginated scrollback (WHERE thread_id = ? ORDER BY created_at) and
        # plain thread_id-only lookups (leftmost prefix) — replaces the standalone
        # thread_id index below, which would be redundant with this.
        Index("ix_chat_messages_thread_created", "thread_id", "created_at"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("chat_threads.thread_id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ChatAnalytics(Base):
    """One row per completed assistant turn — real token usage (summed from the OpenAI Agents
    SDK's own per-response Usage, not the char-based estimate _message_out uses for the UI's
    live token_count display) plus an estimated cost. Every row is self-contained
    (project/platform/model denormalized), same as AuditLog.
    """

    __tablename__ = "chat_analytics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("chat_threads.thread_id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), index=True)
    project: Mapped[str] = mapped_column(String, nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # How many of input_tokens Azure served from its own prompt cache (a repeated identical
    # prefix, e.g. this turn's system prompt + tool schemas across its own multi-step
    # tool-calling loop, is billed at a discount automatically). A subset of input_tokens, not
    # a separate pool — kept out of total_tokens' sum for that reason. estimated_cost prices
    # it at the cheaper rate.
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    @property
    def total_tokens(self) -> int:
        """Derived, not stored — always input_tokens + output_tokens. A Python property
        makes it impossible for the two to drift."""
        return self.input_tokens + self.output_tokens


class MessageFeedback(Base):
    """Thumbs up/down on a specific assistant message. One row per (message_id, user_id): a
    second click from the same user overwrites their prior rating rather than accumulating
    duplicate rows, matching the frontend's own toggle-off-the-other/toggle-off-itself
    interaction.
    """

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "user_id", name="uq_message_feedback_message_user"
        ),
        CheckConstraint("rating IN ('up', 'down')", name="ck_message_feedback_rating"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_messages.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    rating: Mapped[str] = mapped_column(String, nullable=False)  # up | down
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
